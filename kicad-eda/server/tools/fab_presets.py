"""Fab presets and capabilities (ADR D7.2.4, D7.3).

Data: ``server/data/fabs/<fab>.json`` (schema ``kicad-eda/fabs@1``): sources,
``capabilities`` (layer counts, impedance layer counts / tolerance, thicknesses
and their tolerance, copper weights, materials offered, ``rf`` {materials,
layer_counts}, ``hybrid``, ``ptfe_multilayer``), ``soldermask`` (C1/C2/C3 and
Er; omitted when the fab publishes none), ``dk_basis``, ``default_templates``
and ``templates``. A fab may pull a bulk template file (``bulk_templates``):
``jlcpcb_templates.json`` (schema ``kicad-eda/fab_templates@1``) holds the 577
public-domain JLC stackups converted by ``convert_jlc_stackups.py``.

A template is a compact stack, top to bottom: ``{"cu": mm, "oz": w}`` copper
and ``{"pp"|"core": material id, "c": construction, "t": mm}`` dielectric
plies (consecutive plies between two coppers form one dielectric layer).
``expand_preset("<fab>:<template>")`` turns it into the D7.2.2 layer list a
designer could write (roles from D7.2.4, no Dk: the materials library resolves
Dk), so a preset is a convenience, never a limit.

``capability_problems`` returns the ST-W3 warnings; ``default_template`` picks
the D7.2.4 geometry (LookupError = lint ST-E4).

Stdlib only and Python 3.11-compatible: KiCad's bundled Python imports it.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

import materials as mat

FABS_SCHEMA = "kicad-eda/fabs@1"
TEMPLATES_SCHEMA = "kicad-eda/fab_templates@1"
DEFAULT_DIR = Path(__file__).resolve().parents[1] / "data" / "fabs"
ROLES = ("signal", "plane", "mixed", "unused")
MAX_COPPER = 32
THICKNESS_MATCH_MM = 0.05          # a by-count default must be this close to the ask
PREFERRED_OZ = (1, 0.5)            # NOREQ fallback: 1 oz outer / 0.5 oz inner first
_S, _P, _M, _U = ROLES[0], ROLES[1], ROLES[2], ROLES[3]


@dataclass(frozen=True)
class Problem:
    """One fab-capability warning (lint ST-W3)."""
    id: str
    code: str        # layer_count | impedance_layer_count | material | rf_layer_count | hybrid
    message: str     # | ptfe_multilayer

    def __str__(self) -> str:
        return f"{self.id}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "code": self.code, "message": self.message}


@dataclass(frozen=True, eq=False)
class FabSet:
    """The loaded fabs. ``data_version`` = sha256 over every data file in the
    directory (sorted by name; name + NUL + bytes + NUL each)."""
    fabs: Mapping[str, Mapping[str, Any]]
    template_index: Mapping[str, Mapping[str, Mapping[str, Any]]]
    attributions: Mapping[str, Mapping[str, Any]]      # bulk file name -> attribution
    files: tuple[str, ...]
    data_version: str

    def get(self, fab: str) -> dict[str, Any] | None:
        f = self.fabs.get(fab)
        return copy.deepcopy(dict(f)) if f is not None else None

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.fabs))

    def __contains__(self, fab: object) -> bool:
        return fab in self.fabs

    def templates(self, fab: str) -> tuple[str, ...]:
        return tuple(sorted(self.template_index.get(fab, {})))

    def template(self, fab: str, name: str) -> dict[str, Any] | None:
        """A deep copy of one template, ``default_roles`` filled in (D7.2.4)."""
        tpl = self.template_index.get(fab, {}).get(name)
        if tpl is None:
            return None
        out = copy.deepcopy(dict(tpl))
        out.setdefault("default_roles", list(role_pattern(out["copper_layers"])))
        return out


# ---------------------------------------------------------------------------
# D7.2.4 role table
# ---------------------------------------------------------------------------
def role_pattern(copper_layers: int) -> tuple[str, ...]:
    """Default roles top to bottom (D7.2.4): 1 = S/U, 2 = S/M (B.Cu mixed GND),
    4 = S P P S, 6 = S P S S P S, 8+ = S P S P P then (S P) repeated, ending S.
    Which net a plane carries is stackup_roles' job (first GND, second the
    first rail, further GND)."""
    n = copper_layers
    if n == 1:
        return (_S, _U)
    if n == 2:
        return (_S, _M)
    if n == 4:
        return (_S, _P, _P, _S)
    if n == 6:
        return (_S, _P, _S, _S, _P, _S)
    if isinstance(n, int) and 8 <= n <= MAX_COPPER and n % 2 == 0:
        return (_S, _P, _S, _P, _P) + (_S, _P) * ((n - 6) // 2) + (_S,)
    raise ValueError(f"copper layer count must be 1 or an even number 2..{MAX_COPPER}, got {n!r}")


def copper_names(copper_layers: int) -> tuple[str, ...]:
    """Positional KiCad names: F.Cu, In1.Cu .. In(n-2).Cu, B.Cu."""
    inner = tuple(f"In{k}.Cu" for k in range(1, copper_layers - 1))
    return ("F.Cu",) + inner + ("B.Cu",)


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------
def _positive(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0


def _copper_problem(e: Mapping[str, Any]) -> str | None:
    if not _positive(e.get("cu")) or not _positive(e.get("oz")):
        return "a copper entry needs a positive thickness 'cu' and weight 'oz'"
    return None


def _ply_problem(e: Mapping[str, Any]) -> str | None:
    kinds = [k for k in ("pp", "core") if k in e]
    if len(kinds) != 1 or not isinstance(e[kinds[0]], str):
        return "a dielectric ply needs exactly one of pp/core (a material id)"
    if not isinstance(e.get("c"), str) or not e["c"]:
        return "a dielectric ply needs a construction 'c'"
    if not _positive(e.get("t")):
        return "a dielectric ply needs a positive thickness 't'"
    return None


def _stack_problem(tpl: Mapping[str, Any]) -> str | None:
    stack = tpl.get("stack")
    n = tpl.get("copper_layers")
    if not isinstance(stack, list) or not stack or not isinstance(n, int):
        return "needs copper_layers and a non-empty stack"
    if not all(isinstance(e, dict) for e in stack) or not _positive(tpl.get("thickness_mm")):
        return "stack entries must be objects and thickness_mm positive"
    if "cu" not in stack[0] or "cu" not in stack[-1]:
        return "the stack must start and end with copper"
    for prev, cur in zip(stack, stack[1:], strict=False):
        if "cu" in prev and "cu" in cur:
            return "two coppers with no dielectric between them"
    for e in stack:
        problem = _copper_problem(e) if "cu" in e else _ply_problem(e)
        if problem:
            return problem
    if sum(1 for e in stack if "cu" in e) != n:
        return f"the stack has a copper count other than copper_layers {n}"
    roles = tpl.get("default_roles")
    if roles is not None and (len(roles) != n or any(r not in ROLES for r in roles)):
        return f"default_roles must give one of {ROLES} per copper layer"
    return None


def _add_templates(index: dict[str, dict], fab: str, templates: Any, origin: str) -> None:
    if not isinstance(templates, dict):
        raise ValueError(f"{origin}: templates must be an object")
    for tid, tpl in templates.items():
        if tid in index:
            raise ValueError(f"fab {fab!r}: duplicate template id {tid!r} "
                             f"({index[tid]['origin']} and {origin})")
        problem = _stack_problem(tpl) if isinstance(tpl, dict) else "must be an object"
        if problem:
            raise ValueError(f"{origin}: template {tid!r}: {problem}")
        index[tid] = {**tpl, "origin": origin}


def _read(files: list[Path]) -> tuple[dict[str, dict], dict[str, dict], str]:
    digest = hashlib.sha256()
    fabs: dict[str, dict] = {}
    bulk: dict[str, dict] = {}
    for path in files:
        raw = path.read_bytes()
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(raw + b"\0")
        doc = json.loads(raw.decode("utf-8"))
        schema = doc.get("schema") if isinstance(doc, dict) else None
        if schema == TEMPLATES_SCHEMA:
            bulk[path.name] = doc
        elif schema == FABS_SCHEMA:
            fid = doc.get("id")
            if not isinstance(fid, str) or fid in fabs:
                raise ValueError(f"{path.name}: missing or duplicate fab id {fid!r}")
            fabs[fid] = {**doc, "_file": path.name}
        else:
            raise ValueError(f"{path.name}: schema must be {FABS_SCHEMA!r} or "
                             f"{TEMPLATES_SCHEMA!r}")
    return fabs, bulk, digest.hexdigest()


def load_fabs(root: str | Path | None = None) -> FabSet:
    """Load every ``*.json`` under ``root`` (default server/data/fabs). Raises
    ValueError on a wrong schema, a malformed template, a duplicate fab or
    template id, or a bulk template file no fab references."""
    base = Path(root) if root is not None else DEFAULT_DIR
    files = sorted(base.glob("*.json"), key=lambda p: p.name)
    if not files:
        raise ValueError(f"no fab files under {base}")
    fabs, bulk, version = _read(files)
    index: dict[str, dict[str, dict]] = {}
    used: set[str] = set()
    for fid, doc in fabs.items():
        index[fid] = {}
        _add_templates(index[fid], fid, doc.get("templates", {}), doc["_file"])
        ref = (doc.get("bulk_templates") or {}).get("file")
        if ref is None:
            continue
        if ref not in bulk or bulk[ref].get("fab") != fid:
            raise ValueError(f"{doc['_file']}: bulk template file {ref!r} missing or not "
                             f"for fab {fid!r}")
        _add_templates(index[fid], fid, bulk[ref].get("templates"), ref)
        used.add(ref)
    orphans = sorted(set(bulk) - used)
    if orphans:
        raise ValueError(f"bulk template files no fab references: {orphans}")
    frozen = {k: MappingProxyType({x: y for x, y in v.items() if x != "_file"})
              for k, v in fabs.items()}
    return FabSet(MappingProxyType(frozen),
                  MappingProxyType({k: MappingProxyType(v) for k, v in index.items()}),
                  MappingProxyType({k: v.get("attribution", {}) for k, v in bulk.items()}),
                  tuple(str(p) for p in files), version)


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------
def parse_preset(preset: str) -> tuple[str, str]:
    """``"<fab>:<template>"`` -> (fab, template); ValueError when malformed."""
    if not isinstance(preset, str) or preset.count(":") != 1:
        raise ValueError(f"preset must be '<fab>:<template>', got {preset!r}")
    fab, name = (s.strip() for s in preset.split(":"))
    if not fab or not name:
        raise ValueError(f"preset must be '<fab>:<template>', got {preset!r}")
    return fab, name


def _dielectric(group: list[Mapping[str, Any]]) -> dict[str, Any]:
    plies = [{"material": e.get("pp") or e.get("core"), "construction": e["c"],
              "thickness": e["t"]} for e in group]
    kind = "core" if all("core" in e for e in group) else "prepreg"
    if len(plies) == 1:
        return {"type": "dielectric", "kind": kind, **plies[0]}
    return {"type": "dielectric", "kind": kind, "plies": plies}


def _expand_stack(stack: list[Mapping[str, Any]], roles: tuple[str, ...]) -> list[dict]:
    names = copper_names(len(roles))
    layers: list[dict] = []
    group: list[Mapping[str, Any]] = []
    i = 0
    for e in stack:
        if "cu" not in e:
            group.append(e)
            continue
        if group:
            layers.append(_dielectric(group))
            group = []
        layers.append({"type": "copper", "name": names[i], "oz": e["oz"], "thickness": e["cu"],
                       "role": roles[i], "net": None})
        i += 1
    return layers


def expand_preset(fabs: FabSet, preset: str, *,
                  roles: Iterable[str] | None = None) -> dict[str, Any]:
    """``"<fab>:<template>"`` -> a fresh dict: preset, fab, template,
    copper_layers, board_thickness (the ordered nominal), layers (D7.2.2 form;
    copper roles = ``roles`` or the template's D7.2.4 defaults, nets None),
    default_roles, attribution (bulk templates) and note. LookupError for an
    unknown fab or template (ST-E4), ValueError for a malformed preset/roles."""
    fab, name = parse_preset(preset)
    if fab not in fabs:
        raise LookupError(f"unknown fab {fab!r} in preset {preset!r}")
    tpl = fabs.template(fab, name)
    if tpl is None:
        raise LookupError(f"fab {fab!r} has no template {name!r}")
    n = tpl["copper_layers"]
    use = tuple(roles) if roles is not None else tuple(tpl["default_roles"])
    if len(use) != n or any(r not in ROLES for r in use):
        raise ValueError(f"roles must give one of {ROLES} per copper layer ({n}), got {use!r}")
    attribution = fabs.attributions.get(tpl["origin"])
    return {"preset": f"{fab}:{name}", "fab": fab, "template": name, "copper_layers": n,
            "board_thickness": tpl["thickness_mm"], "layers": _expand_stack(tpl["stack"], use),
            "default_roles": list(tpl["default_roles"]),
            "attribution": copy.deepcopy(dict(attribution)) if attribution else None,
            "note": tpl.get("note")}


def _thickness_of(fabs: FabSet, preset: str) -> float | None:
    fab, name = parse_preset(preset)
    tpl = fabs.template_index.get(fab, {}).get(name)
    return float(tpl["thickness_mm"]) if tpl is not None else None


def _fallback(fabs: FabSet, fab: str, prefix: str, n: int, thickness: float) -> str | None:
    best: tuple | None = None
    for tid, tpl in fabs.template_index.get(fab, {}).items():
        if not tid.startswith(prefix + "-") or tpl["copper_layers"] != n:
            continue
        oz = (tpl.get("outer_oz"), tpl.get("inner_oz"))
        key = (abs(float(tpl["thickness_mm"]) - thickness), oz[0] != PREFERRED_OZ[0],
               oz[1] != PREFERRED_OZ[1], tid)
        best = key if best is None or key < best else best
    return f"{fab}:{best[-1]}" if best else None


def default_template(fabs: FabSet, fab: str, copper_layers: int,
                     thickness_mm: float = 1.6) -> str:
    """The D7.2.4 default geometry as a preset string (``"jlcpcb:JLC04161H-7628"``):
    the fab's by-count default when its thickness matches, else its fallback
    family (JLC: the NOREQ template of that count, nearest thickness). Roles
    come from role_pattern. LookupError when none exists (lint ST-E4)."""
    if fab not in fabs:
        raise LookupError(f"unknown fab {fab!r}")
    try:
        role_pattern(copper_layers)
    except ValueError as exc:
        raise LookupError(f"no template for {copper_layers!r} copper layers: {exc}") from exc
    rules = fabs.fabs[fab].get("default_templates") or {}
    cand = (rules.get("by_count") or {}).get(str(copper_layers))
    if cand:
        preset = cand if ":" in cand else f"{fab}:{cand}"
        t = _thickness_of(fabs, preset)
        if t is not None and abs(t - thickness_mm) <= THICKNESS_MATCH_MM:
            return preset
    if rules.get("fallback"):
        found = _fallback(fabs, fab, rules["fallback"], copper_layers, thickness_mm)
        if found:
            return found
    raise LookupError(f"fab {fab!r} has no template for {copper_layers} copper layers at "
                      f"{thickness_mm:g} mm (declare stackup.layers, or use jlcpcb)")


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------
def _caps(fabs: FabSet, fab: str) -> Mapping[str, Any]:
    if fab not in fabs:
        raise LookupError(f"unknown fab {fab!r}")
    return fabs.fabs[fab].get("capabilities") or {}


def soldermask(fabs: FabSet, fab: str | None) -> dict[str, Any] | None:
    """The fab's published solder mask (over_substrate / over_trace /
    between_traces mm, dk, source), or None (no fab, or none published: the
    caller then uses materials.generic_mask, D7.2.5)."""
    if fab is None:
        return None
    if fab not in fabs:
        raise LookupError(f"unknown fab {fab!r}")
    mask = fabs.fabs[fab].get("soldermask")
    return copy.deepcopy(dict(mask)) if mask else None


def controls_impedance(fabs: FabSet, fab: str | None, copper_layers: int) -> bool:
    """True when the fab controls impedance on this layer count (dk_policy auto
    then uses the fab's own Dk)."""
    if fab is None:
        return False
    caps = _caps(fabs, fab)
    counts = caps.get("impedance_layer_counts")
    if counts is not None:
        return copper_layers in counts
    return caps.get("impedance_tolerance_pct") is not None


def impedance_min_layers(fabs: FabSet, fab: str | None) -> int | None:
    """The smallest layer count the fab controls impedance on (JLC: 4), or None."""
    if fab is None:
        return None
    counts = _caps(fabs, fab).get("impedance_layer_counts")
    return min(counts) if counts else None


def thickness_tolerance_mm(fabs: FabSet, fab: str | None, nominal_mm: float) -> float | None:
    """The fab's finished-thickness tolerance at ``nominal_mm`` (lint ST-W1), or None."""
    if fab is None:
        return None
    rules = _caps(fabs, fab).get("thickness_tolerance") or []
    for rule in sorted(rules, key=lambda r: -r["from_mm"]):
        if nominal_mm >= rule["from_mm"]:
            return nominal_mm * rule["pct"] / 100.0 if "pct" in rule else float(rule["abs_mm"])
    return None


def _offered(mid: str, m: Mapping[str, Any], allowed: list[str] | None) -> bool:
    for item in allowed or ():
        kind, _, value = item.partition(":")
        if (item == mid or (kind == "category" and m.get("category") == value)
                or (kind == "family" and m.get("family") == value)):
            return True
    return False


def _material_problems(name: str, caps: Mapping[str, Any], n: int,
                       known: list[tuple[str, Mapping[str, Any]]]) -> list[Problem]:
    std, rf = caps.get("materials"), caps.get("rf") or {}
    if std is None and rf.get("materials") is None:
        return []
    out: list[Problem] = []
    for mid, m in known:
        if _offered(mid, m, std):
            continue
        if _offered(mid, m, rf.get("materials")):
            counts = rf.get("layer_counts")
            if counts is not None and n not in counts:
                out.append(Problem("ST-W3", "rf_layer_count", (
                    f"{name} builds {m.get('product', mid)} only on {counts}-layer RF boards, "
                    f"not {n} layers")))
            continue
        out.append(Problem("ST-W3", "material",
                           f"{name} does not list {m.get('product', mid)} ({mid})"))
    return out


def _mix_problems(name: str, caps: Mapping[str, Any], n: int,
                  known: list[tuple[str, Mapping[str, Any]]]) -> list[Problem]:
    rf = [mid for mid, m in known if m.get("category") == "rf-laminate"]
    other = [mid for mid, m in known if m.get("category") in ("fr4", "high-speed")]
    out: list[Problem] = []
    if rf and other and caps.get("hybrid") is False:
        out.append(Problem("ST-W3", "hybrid", (
            f"{name} does not build hybrid stackups ({', '.join(rf)} with {', '.join(other)})")))
    if n > 2 and known and all(m.get("ptfe") for _, m in known) \
            and caps.get("ptfe_multilayer") is False:
        out.append(Problem("ST-W3", "ptfe_multilayer", (
            f"{name} cannot laminate a pure-PTFE {n}-layer board "
            f"({', '.join(mid for mid, _ in known)}); add an FR-4 or RO4000 layer")))
    return out


def capability_problems(fabs: FabSet, fab: str | None, *, copper_layers: int,
                        materials: Iterable[str] = (), impedance_required: bool = False,
                        lib: mat.MaterialLibrary | None = None) -> tuple[Problem, ...]:
    """ST-W3 warnings for a build at ``fab`` (D7.2.7): layer count not offered;
    impedance required on a count the fab does not control (JLC: 4-32 only);
    a material not offered, or an RF material outside its RF layer counts; a
    hybrid at a fab with ``hybrid: false``; a pure-PTFE multilayer where the
    fab cannot laminate one. ``materials`` are library ids (labels the library
    does not know and masks are skipped). No fab -> (); unknown fab -> LookupError."""
    if fab is None:
        return ()
    caps = _caps(fabs, fab)
    name = fabs.fabs[fab].get("name", fab)
    ids = [m for m in dict.fromkeys(materials) if m]
    library = lib if lib is not None else (mat.load_library() if ids else None)
    known = [(mid, library.materials[mid]) for mid in ids
             if library is not None and mid in library
             and library.materials[mid].get("category") != "mask"]
    out: list[Problem] = []
    counts = caps.get("layer_counts")
    if counts is not None and copper_layers not in counts:
        out.append(Problem("ST-W3", "layer_count",
                           f"{name} does not build {copper_layers} copper layers ({counts})"))
    imp = caps.get("impedance_layer_counts")
    if impedance_required and imp is not None and copper_layers not in imp:
        out.append(Problem("ST-W3", "impedance_layer_count", (
            f"{name} controls impedance only on {min(imp)}-{max(imp)} layers: on "
            f"{copper_layers} layers the impedance is uncontrolled")))
    out += _material_problems(name, caps, copper_layers, known)
    out += _mix_problems(name, caps, copper_layers, known)
    return tuple(out)
