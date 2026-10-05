"""The layers of a stackup: names, KiCad ids, the ``stackup.layers`` list grammar,
roles, plane nets, the single-sided rules and the per-layer build checks (ADR
D7.2.1-D7.2.7). stackup_model resolves a whole stackup on top of it.

* ``default_roles(n, design)``: the D7.2.4 roles of an n-layer board (1 = F signal
  / B unused, 2 = F signal / B mixed GND, 4 = S G P S, 6 = S G S S P S, 8+ = S G S
  P G then (S G) repeated, ending S), plane nets filled in.
* ``default_plane_nets(layers, design)``: a plane or mixed layer with no net gets
  one, recorded as a G1 decision draft (confidence med). This is the heuristic
  that lived in swig_build_board.py:389-397 (In1 GND, In2 the first power rail:
  a net starting with '+', else the first containing 'V'), generalised: the
  defaults GND, then the first rail, go to the undeclared plane/mixed layers top
  to bottom, skipping a default some layer already declares; every further layer
  gets GND, and so does the second one when the design has no rail (M7).
* ``apply_overrides(layers, roles=, planes=)``: the preset ``stackup.roles``
  overrides and the netlist ``planes`` assignment. Errors are ST-E6 issues.
* ``copper_entry``: one ``stackup.layers`` copper entry (oz 1 outer / 0.5 inner,
  thickness oz x 0.035 mm, positional name: ST-E2, values: ST-E5, role: ST-E6).
* ``parse_layers``: a D7.2.2 layer list (copper/dielectric alternating, copper
  first and last: ST-E1; an even copper count: ST-E2) into copper entries, roles
  and raw dielectrics (one ply or ``plies``); ``ply_problems`` checks ply values.
* ``single_sided_issues(design)``: ST-E12 (D7.2.6) for a board whose B.Cu is unused.
* ``asymmetry`` (ST-W2) and ``copper_fill_issues`` (ST-W4, bondply fill per ply).

A role is ``explicit`` when the designer declared it (a ``layers`` entry or a
``stackup.roles`` override) and defaulted otherwise. ``planes`` may turn a
defaulted signal layer into a plane (an outer one into a mixed layer: an outer
layer carries every SMD pad, so it can never be single-net copper); naming an
explicit signal layer there is an error.

``Issue`` (id, severity, message, JSON path rendered like jsonschema's
``json_path``) and the small validators are shared with stackup_model. Stdlib
only and Python 3.11-compatible: KiCad's bundled Python imports it.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping, Sequence

import fab_presets as fp
import materials as mat

ROLES = fp.ROLES                                   # signal | plane | mixed | unused
NET_ROLES = ("plane", "mixed")
ROUTABLE = ("signal", "mixed")
OUTER = ("F.Cu", "B.Cu")
KICAD_TYPES = {"signal": "signal", "plane": "power", "mixed": "mixed", "unused": "signal"}
OZ_MM = 0.035
OUTER_OZ, INNER_OZ = 1, 0.5
COPPER_KEYS = frozenset({"type", "name", "oz", "thickness", "role", "net"})
PLY_KEYS = frozenset({"type", "thickness", "material", "construction", "dk", "df", "dk_freq_hz",
                      "label", "locked", "dielectric_model"})
DIELECTRIC_KEYS = PLY_KEYS | {"kind", "plies"}
KINDS = ("core", "prepreg")
DK_RANGE = (1.0, 15.0)
DF_RANGE = (0.0, 0.1)
# Footprints taken as through-hole: ST-E12 lets them sit on side B of a single-sided board
THT_LIBRARY_PREFIXES = ("Package_DIP", "Package_TO_SOT_THT", "Connector_PinHeader",
                        "Connector_PinSocket", "Connector_IDC", "TerminalBlock", "MountingHole")
DRAFT_SOURCE = "ADR D7.2.3 default plane nets (stackup_roles.default_plane_nets)"
ROUND = 6
_PATH_KEY = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")  # jsonschema's json_path rule
_INNER = re.compile(r"^In([1-9][0-9]?)\.Cu$")
_OVERRIDE_KEYS = frozenset({"role", "net"})
Path = tuple                    # a JSON path as its elements (rendered by json_path)
At = Callable[..., Path]        # at(entry index, *sub keys) -> the JSON path


# ---------------------------------------------------------------------------
# Issues and small validators
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Issue:
    """One G1 lint finding: ``id`` (ST-E*/ST-W*), ``severity`` (error | warning |
    info), a message and the JSON path it is about."""
    id: str
    severity: str
    message: str
    path: str

    def __str__(self) -> str:
        return f"{self.id} at {self.path}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "severity": self.severity, "message": self.message,
                "path": self.path}


def json_path(*elems: str | int) -> str:
    """``$.a.b[2]['In1.Cu']``: the jsonschema ``json_path`` rendering of a path."""
    out = "$"
    for e in elems:
        if isinstance(e, int):
            out += f"[{e}]"
        elif _PATH_KEY.match(e):
            out += "." + e
        else:
            out += "['" + e.replace("\\", "\\\\").replace("'", "\\'") + "']"
    return out


def error(ident: str, message: str, path: Path) -> Issue:
    return Issue(ident, "error", message, json_path(*path))


def warning(ident: str, message: str, path: Path, severity: str = "warning") -> Issue:
    return Issue(ident, severity, message, json_path(*path))


def has_errors(issues: Iterable[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)


def is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def positive(value: Any, path: Path, what: str) -> list[Issue]:
    """[] or one ST-E5 issue."""
    if is_number(value) and value > 0:
        return []
    return [error("ST-E5", f"{what} must be a positive number, got {value!r}", path)]


def in_range(value: Any, lo: float, hi: float, path: Path, what: str) -> list[Issue]:
    """[] or one ST-E5 issue."""
    if is_number(value) and lo <= value <= hi:
        return []
    return [error("ST-E5", f"{what} must be a number in [{lo:g}, {hi:g}], got {value!r}", path)]


def unknown_keys(entry: Mapping[str, Any], allowed: frozenset, path: Path,
                 what: str) -> list[Issue]:
    """One ST-E3 issue per key outside ``allowed`` (the model has a fixed shape)."""
    return [error("ST-E3", f"unknown {what} key {k!r} (allowed: {', '.join(sorted(allowed))})",
                  (*path, str(k))) for k in sorted(set(entry) - allowed, key=str)]


def _role_err(message: str, path: Path) -> Issue:
    return error("ST-E6", message, path)


# ---------------------------------------------------------------------------
# Counts, names and ids
# ---------------------------------------------------------------------------
def check_count(n: Any) -> int:
    """``n`` when it is a valid design copper count (1, or even 2..32), else ValueError."""
    if isinstance(n, bool) or not isinstance(n, int) or not (n == 1 or (
            2 <= n <= fp.MAX_COPPER and n % 2 == 0)):
        raise ValueError(f"copper layer count must be 1 or an even number 2..{fp.MAX_COPPER}, "
                         f"got {n!r}")
    return n


def kicad_count(n: int) -> int:
    """The KiCad copper count of a design count (single-sided = a 2-layer board)."""
    return max(2, check_count(n))


def copper_names(n: int) -> tuple[str, ...]:
    """The KiCad copper names of a design count, top to bottom."""
    return fp.copper_names(kicad_count(n))


def kicad_layer_id(name: str) -> int:
    """KiCad 9+ copper layer ids: F.Cu 0, B.Cu 2, In<k>.Cu 2k + 2 (k 1..30)."""
    if name in OUTER:
        return 0 if name == "F.Cu" else 2
    m = _INNER.match(name) if isinstance(name, str) else None
    if m is None or int(m.group(1)) > fp.MAX_COPPER - 2:
        raise ValueError(f"not a KiCad copper layer name: {name!r}")
    return 2 * int(m.group(1)) + 2


# ---------------------------------------------------------------------------
# Layer roles and copper entries
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LayerRole:
    """The role and net of one copper layer. ``net_source``: declared (in the
    stackup), planes (the netlist ``planes`` key), default (D7.2.3) or None."""
    name: str
    role: str
    net: str | None = None
    explicit: bool = False
    net_source: str | None = None


@dataclass(frozen=True)
class CopperEntry:
    """A copper layer's name, weight (oz) and thickness (mm)."""
    name: str
    oz: float
    thickness: float


def _copper_values(e: Mapping[str, Any], name: str, i: int, at: At
                   ) -> tuple[CopperEntry, list[Issue]]:
    issues = unknown_keys(e, COPPER_KEYS, at(i), "copper layer")
    if e.get("name") is not None and e["name"] != name:
        issues.append(error("ST-E2", f"copper names are positional: this one is {name}, not "
                                     f"{e['name']!r}", at(i, "name")))
    oz, thickness = e.get("oz"), e.get("thickness")
    if oz is not None:
        issues += positive(oz, at(i, "oz"), "oz")
    if thickness is not None:
        issues += positive(thickness, at(i, "thickness"), "copper thickness")
    if issues:
        return CopperEntry(name, OUTER_OZ, OZ_MM), issues
    if thickness is None:
        oz = oz if oz is not None else (OUTER_OZ if name in OUTER else INNER_OZ)
        return CopperEntry(name, oz, round(oz * OZ_MM, ROUND)), []
    weight = oz if oz is not None else round(thickness / OZ_MM, 3)
    return CopperEntry(name, weight, round(float(thickness), ROUND)), []


def _entry_role(e: Mapping[str, Any], name: str, default: str, n: int, i: int, at: At,
                explicit: bool) -> tuple[LayerRole, list[Issue]]:
    role, net = e.get("role"), e.get("net")
    if role is None and net is None:
        return LayerRole(name, default), []
    given = role is not None
    role = role if given else default
    if role not in ROLES or (role == "unused" and not (name == "B.Cu" and n == 2)):
        return LayerRole(name, default), [_role_err(
            f"role must be one of {ROLES} ('unused' only for B.Cu of a single-sided board), "
            f"got {role!r}", at(i, "role"))]
    if net is not None and (not isinstance(net, str) or not net.strip()):
        return LayerRole(name, role), [_role_err(f"net must be a net name, got {net!r}",
                                                 at(i, "net"))]
    if net is not None and role not in NET_ROLES:
        return LayerRole(name, role), [_role_err(
            f"a {role} layer carries no net: only plane and mixed layers do", at(i, "net"))]
    return LayerRole(name, role, net, explicit and given, "declared" if net else None), []


def copper_entry(e: Mapping[str, Any], name: str, default_role: str, n: int, i: int, at: At,
                 explicit: bool) -> tuple[CopperEntry, LayerRole, list[Issue]]:
    """One ``stackup.layers`` copper entry at list index ``i`` of an n-copper
    stack: its values and role (``default_role`` when it gives none)."""
    cu, issues = _copper_values(e, name, i, at)
    role, more = _entry_role(e, name, default_role, n, i, at, explicit)
    return cu, role, issues + more


# ---------------------------------------------------------------------------
# The stackup.layers list (also expanded presets and default templates)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RawPly:
    """One ply as declared (``spec``), where it was declared and its dielectric kind."""
    spec: Mapping[str, Any]
    path: Path
    kind: str


@dataclass(frozen=True)
class RawDielectric:
    kind: str
    locked: bool
    plies: tuple[RawPly, ...]


@dataclass(frozen=True)
class ParsedLayers:
    """A parsed layer list; ``count`` is the design count (1 = single-sided)."""
    coppers: tuple[CopperEntry, ...]
    roles: tuple[LayerRole, ...]
    dielectrics: tuple[RawDielectric, ...]
    count: int


def _structure(layers: Any, at: At) -> list[Issue]:
    if not isinstance(layers, list) or not layers:
        return [error("ST-E1", "stackup.layers is a non-empty list of copper and dielectric "
                               "entries, top to bottom", ("stackup", "layers"))]
    issues = [error("ST-E1", "a layer entry is an object", at(i)) for i, e in enumerate(layers)
              if not isinstance(e, Mapping)]
    issues += [error("ST-E1", f"type must be copper or dielectric, got {e.get('type')!r}",
                     at(i, "type")) for i, e in enumerate(layers)
               if isinstance(e, Mapping) and e.get("type") not in ("copper", "dielectric")]
    if issues:
        return issues
    types = [e["type"] for e in layers]
    ends = sorted({i for i in (0, len(types) - 1) if types[i] != "copper"})
    return [error("ST-E1", "the stack starts and ends with copper", at(i)) for i in ends] + [
        error("ST-E1", f"two {types[i]} layers in a row: copper and dielectric alternate", at(i))
        for i in range(1, len(types)) if types[i] == types[i - 1]]


def _plies(e: Mapping[str, Any], kind: str, i: int, at: At) -> tuple[tuple, list[Issue]]:
    plies = e["plies"]
    issues = ([error("ST-E1", "a multi-ply dielectric gives its values per ply", at(i))]
              if set(e) & (PLY_KEYS - {"type", "locked"}) else [])
    if not isinstance(plies, list) or not plies:
        return (), issues + [error("ST-E1", "plies is a non-empty list", at(i, "plies"))]
    for j, p in enumerate(plies):
        issues += ([error("ST-E1", "a ply is an object", at(i, "plies", j))]
                   if not isinstance(p, Mapping) else
                   unknown_keys(p, PLY_KEYS, at(i, "plies", j), "ply"))
    return tuple(RawPly(p, at(i, "plies", j), kind) for j, p in enumerate(plies)), issues


def _dielectric(e: Mapping[str, Any], i: int, at: At) -> tuple[RawDielectric | None, list[Issue]]:
    issues = unknown_keys(e, DIELECTRIC_KEYS, at(i), "dielectric")
    kind = e.get("kind") or ("prepreg" if "plies" in e else "core")
    if kind not in KINDS:
        issues.append(error("ST-E1", f"kind must be core or prepreg, got {kind!r}",
                            at(i, "kind")))
    locked = e.get("locked", False)
    if not isinstance(locked, bool):
        issues.append(error("ST-E3", f"locked must be true or false, got {locked!r}",
                            at(i, "locked")))
    if "plies" not in e:
        if "thickness" not in e:
            issues.append(error("ST-E1", "a dielectric needs a thickness (or plies)", at(i)))
        return RawDielectric(kind, locked is True, (RawPly(e, at(i), kind),)), issues
    raw, more = _plies(e, kind, i, at)
    if issues or more:
        return None, issues + more
    return RawDielectric(kind, locked is True or any(p.spec.get("locked") is True for p in raw),
                         raw), []


def parse_layers(layers: Any, *, base: Path | None, explicit: bool, declared: int | None
                 ) -> tuple[ParsedLayers | None, list[Issue]]:
    """Parse a D7.2.2 layer list. Paths point at each entry when ``base`` is None,
    else at ``base`` (a preset). ``explicit``: given roles are the designer's;
    ``declared``: the netlist copper_layers (1 makes a 2-copper stack single-sided)."""
    def at(i: int, *rest: str | int) -> Path:
        return base if base is not None else ("stackup", "layers", i, *rest)
    issues = _structure(layers, at)
    if issues:
        return None, issues
    cu_idx = [i for i, e in enumerate(layers) if e["type"] == "copper"]
    n = len(cu_idx)
    if n < 2 or n > fp.MAX_COPPER or n % 2:
        return None, [error("ST-E2", f"{n} copper layers: a stack has an even number 2.."
                                     f"{fp.MAX_COPPER} (single-sided = 2 with B.Cu role unused)",
                            base or ("stackup", "layers"))]
    b_role = layers[cu_idx[-1]].get("role")
    single = n == 2 and (b_role == "unused" or (declared == 1 and b_role is None))
    pattern, names = fp.role_pattern(1 if single else n), fp.copper_names(n)
    coppers, layer_roles, dies = [], [], []
    for i, e in enumerate(layers):
        if e["type"] == "copper":
            k = len(coppers)
            cu, role, more = copper_entry(e, names[k], pattern[k], n, i, at, explicit)
            coppers.append(cu)
            layer_roles.append(role)
        else:
            die, more = _dielectric(e, i, at)
            dies.append(die)
        issues += more
    if issues:
        return None, issues
    return ParsedLayers(tuple(coppers), tuple(layer_roles), tuple(dies), 1 if single else n), []


def ply_problems(s: Mapping[str, Any], path: Path) -> list[Issue]:
    """ST-E1/E3/E5 problems of one ply's values (thickness, dk, df, dk_freq_hz, text keys)."""
    issues = positive(s.get("thickness"), (*path, "thickness"), "dielectric thickness")
    if s.get("type") not in (None, "dielectric"):
        issues.append(error("ST-E1", f"a ply is dielectric, got type {s.get('type')!r}",
                            (*path, "type")))
    for key, lo_hi in (("dk", DK_RANGE), ("df", DF_RANGE)):
        if s.get(key) is not None:
            issues += in_range(s[key], *lo_hi, (*path, key), key)
    if s.get("dk_freq_hz") is not None:
        issues += positive(s["dk_freq_hz"], (*path, "dk_freq_hz"), "dk_freq_hz")
    return issues + [error("ST-E3", f"{k} is text", (*path, k))
                     for k in ("material", "construction", "label", "dielectric_model")
                     if s.get(k) is not None and (not isinstance(s[k], str) or not s[k].strip())]


# ---------------------------------------------------------------------------
# Default roles and plane nets
# ---------------------------------------------------------------------------
def _net_names(design: Mapping[str, Any] | None) -> list[str]:
    nets = (design or {}).get("nets") or ()
    return [n for n in nets if isinstance(n, str)]


def first_power_rail(design: Mapping[str, Any] | None) -> str | None:
    """Today's heuristic: the first net starting with '+' (a regulated logic
    rail: +3V3, +5V, +1V8), else the first containing 'V' (VBUS, VIN), GND excluded."""
    nets = [n for n in _net_names(design) if n != "GND"]
    return (next((n for n in nets if n.startswith("+")), None)
            or next((n for n in nets if "V" in n.upper()), None))


def default_roles(n: int, design: Mapping[str, Any] | None = None) -> tuple[LayerRole, ...]:
    """The D7.2.4 roles of an n-layer design with default plane nets (the drafts
    of default_plane_nets are dropped; call it directly to keep them)."""
    layers = tuple(LayerRole(name, role) for name, role in
                   zip(copper_names(n), fp.role_pattern(check_count(n)), strict=True))
    return default_plane_nets(layers, design)[0]


def _why(kind: str, rail: str | None) -> str:
    if kind == "gnd":
        return ("the first plane/mixed layer without a declared net carries GND, the return "
                "plane (ADR D7.2.3)")
    if kind == "rail" and rail:
        rule = "a net starting with '+'" if rail.startswith("+") else "the first net containing 'V'"
        return (f"the second plane/mixed layer carries the first power rail, {rail} ({rule}; "
                "ADR D7.2.3, the pre-D7 builder default)")
    if kind == "no_rail":
        return ("the design has no power rail (no net starting with '+' or containing 'V'), so "
                "this plane/mixed layer also carries GND (ADR D7.2.3, M7)")
    return "further plane/mixed layers without a declared net carry GND (ADR D7.2.3)"


def _draft(layer: LayerRole, net: str, kind: str, rail: str | None) -> dict[str, Any]:
    others = [x for x in ("GND", rail) if x and x != net]
    return {"stage": "G1", "question": f"Which net does the {layer.role} layer {layer.name} carry?",
            "decision": f"{layer.name} carries {net}", "rationale": _why(kind, rail),
            "alternatives": others + [f"declare planes.{layer.name} or a stackup role net"],
            "confidence": "med", "source": DRAFT_SOURCE, "reversible": True,
            "override": f"planes.{layer.name}"}


def _default_queue(declared: set[str], rail: str | None) -> list[tuple[str, str]]:
    """(kind, net) defaults in the order the undeclared layers take them."""
    queue = [] if "GND" in declared else [("gnd", "GND")]
    if rail is None:
        queue.append(("no_rail", "GND"))
    elif rail not in declared:
        queue.append(("rail", rail))
    return queue


def default_plane_nets(layers: Sequence[LayerRole], design: Mapping[str, Any] | None
                       ) -> tuple[tuple[LayerRole, ...], tuple[dict[str, Any], ...]]:
    """New layers with a net on every plane/mixed layer, and one G1 decision
    draft (decisions.append_decision fields without id/ts) per defaulted net."""
    rail = first_power_rail(design)
    queue = _default_queue({x.net for x in layers if x.role in NET_ROLES and x.net}, rail)
    out: list[LayerRole] = []
    drafts: list[dict[str, Any]] = []
    for layer in layers:
        if layer.role not in NET_ROLES or layer.net:
            out.append(layer)
            continue
        kind, net = queue.pop(0) if queue else ("further", "GND")
        out.append(replace(layer, net=net, net_source="default"))
        drafts.append(_draft(layer, net, kind, rail))
    return tuple(out), tuple(drafts)


# ---------------------------------------------------------------------------
# Overrides: preset stackup.roles and the netlist planes
# ---------------------------------------------------------------------------
def _role_override(layer: LayerRole, spec: Any, n: int,
                   path: Path) -> tuple[LayerRole | None, Issue | None]:
    if not isinstance(spec, Mapping) or set(spec) - _OVERRIDE_KEYS or "role" not in spec:
        return None, _role_err("a role override is {\"role\": signal|plane|mixed|unused, "
                               "\"net\": <net>}", path)
    role, net = spec["role"], spec.get("net")
    if role not in ROLES or (role == "unused" and not (layer.name == "B.Cu" and n == 2)):
        return None, _role_err(f"role {role!r} is not one of {ROLES} ('unused' is only for B.Cu "
                               "of a single-sided board)", (*path, "role"))
    if net is not None and (not isinstance(net, str) or not net.strip()):
        return None, _role_err("a role net must be a net name", (*path, "net"))
    if net is not None and role not in NET_ROLES:
        return None, _role_err(f"a {role} layer carries no net (only plane and mixed layers do)",
                               (*path, "net"))
    return LayerRole(layer.name, role, net, True, "declared" if net else None), None


def _plane_target(layer: LayerRole) -> str:
    if layer.explicit or layer.role != "signal":
        return layer.role
    return "mixed" if layer.name in OUTER else "plane"


def _apply_plane(layer: LayerRole, net: Any, n: int,
                 path: Path) -> tuple[LayerRole | None, Issue | None]:
    if not isinstance(net, str) or not net.strip():
        return None, _role_err("a planes entry is <copper layer>: <net name>", path)
    if layer.role == "unused":
        return None, _role_err(f"{layer.name} is unused on a single-sided board: it carries no "
                               "plane", path)
    role = _plane_target(layer)
    if role == "signal":
        return None, _role_err(f"planes names {layer.name}, which the stackup declares a signal "
                               "layer: make it a plane or mixed layer, or drop it from planes",
                               path)
    if layer.net and layer.net != net:
        return None, _role_err(f"planes gives {layer.name} the net {net} but the stackup gives "
                               f"it {layer.net}", path)
    source = layer.net_source if layer.net else "planes"
    return replace(layer, role=role, net=net, net_source=source), None


def _apply(layers: list[LayerRole], mapping: Any, base: Path, what: str,
           one: Callable[..., tuple[LayerRole | None, Issue | None]]) -> list[Issue]:
    if not isinstance(mapping, Mapping):
        return [_role_err(f"{what} maps copper layer names to entries", base)]
    index = {x.name: i for i, x in enumerate(layers)}
    issues: list[Issue] = []
    for name in sorted(mapping, key=lambda k: (index.get(k, len(index)), str(k))):
        if name not in index:
            issues.append(_role_err(f"{what} names {name!r}, which is not a copper layer of "
                                    f"this stackup ({', '.join(index)})", (*base, str(name))))
            continue
        new, problem = one(layers[index[name]], mapping[name], len(layers), (*base, name))
        if problem:
            issues.append(problem)
        else:
            layers[index[name]] = new
    return issues


def apply_overrides(layers: Iterable[LayerRole], *, roles: Any = None, planes: Any = None,
                    roles_path: Sequence[str] = ("stackup", "roles"),
                    planes_path: Sequence[str] = ("planes",)
                    ) -> tuple[tuple[LayerRole, ...], tuple[Issue, ...]]:
    """Apply preset ``stackup.roles`` overrides, then the netlist ``planes``
    (D7.2.3), to a copy of ``layers``. Returns the new layers and ST-E6 issues;
    an entry with an error is skipped (the rest still apply)."""
    out = list(layers)
    issues: list[Issue] = []
    if roles is not None:
        issues += _apply(out, roles, tuple(roles_path), "stackup.roles", _role_override)
    if planes is not None:
        issues += _apply(out, planes, tuple(planes_path), "planes", _apply_plane)
    return tuple(out), tuple(issues)


# ---------------------------------------------------------------------------
# Single-sided (D7.2.6)
# ---------------------------------------------------------------------------
def impedance_nets(design: Mapping[str, Any]) -> list[str]:
    """Nets with an impedance, Zdiff or RF frequency target (net_specs order)."""
    specs = design.get("net_specs")
    if not isinstance(specs, Mapping):
        return []
    return [n for n, s in specs.items() if isinstance(s, Mapping) and any(
        s.get(k) is not None for k in ("impedance", "diff", "freq_hz"))]


def through_hole(footprint: Any) -> bool:
    """A footprint id taken as through-hole (a THT library or name, or a
    connector / DIP / terminal / hole library)."""
    if not isinstance(footprint, str):
        return False
    lib, _, name = footprint.partition(":")
    return "THT" in lib or "_THT" in name or lib.startswith(THT_LIBRARY_PREFIXES)


def _preroute_issues(preroutes: Any) -> list[Issue]:
    issues: list[Issue] = []
    for i, pr in enumerate(preroutes if isinstance(preroutes, list) else []):
        if isinstance(pr, Mapping) and pr.get("layer") == "B.Cu":
            issues.append(error("ST-E12", "a preroute on B.Cu: B.Cu is unused on a single-sided "
                                          "board", ("preroutes", i, "layer")))
        if isinstance(pr, Mapping) and pr.get("vias"):
            issues.append(error("ST-E12", "vias on a single-sided board (there is no second "
                                          "copper layer)", ("preroutes", i, "vias")))
    return issues


def single_sided_issues(design: Mapping[str, Any]) -> list[Issue]:
    """ST-E12: an SMD part on side B, a preroute or via on B.Cu, ``emc.stitch``,
    or an impedance/RF net (no reference: ST-E9 too) on a single-sided board."""
    comps, emc = design.get("components"), design.get("emc")
    issues = [error("ST-E12", f"{c.get('ref', '?')} ({c.get('footprint')}) is on side B, but a "
                              "single-sided board has no B-side copper for SMD pads: put it on "
                              "side F (only a through-hole part may sit on B)",
                    ("components", i, "side"))
              for i, c in enumerate(comps if isinstance(comps, list) else [])
              if isinstance(c, Mapping) and str(c.get("side") or "F").upper() == "B"
              and not through_hole(c.get("footprint"))]
    issues += _preroute_issues(design.get("preroutes"))
    if isinstance(emc, Mapping) and emc.get("stitch") is True:
        issues.append(error("ST-E12", "EMC stitching needs ground on two layers; a single-sided "
                                      "board has one", ("emc", "stitch")))
    return issues + [error("ST-E12", f"{net} has an impedance/RF target, but a single-sided "
                                     "board has no reference plane (ST-E9): use 2 or more layers",
                           ("net_specs", net)) for net in impedance_nets(design)]


# ---------------------------------------------------------------------------
# Per-layer build checks (ST-W2, ST-W4) on resolved copper and dielectrics
# ---------------------------------------------------------------------------
def _ply_key(plies: Sequence[Any]) -> tuple:
    return tuple((p.material_id or p.label, round(p.thickness, 3), round(p.er, 2)) for p in plies)


def asymmetry(copper: Sequence[Any], dielectrics: Sequence[Any]) -> str | None:
    """Why the build is not mirror-symmetric about its centre (ST-W2), or None:
    copper thickness pairs, then dielectric pairs (material, thickness, Er per ply)."""
    n, m = len(copper), len(dielectrics)
    for k in range(n // 2):
        a, b = copper[k], copper[n - 1 - k]
        if abs(a.thickness - b.thickness) > 1e-3:
            return f"{a.name} copper {a.thickness:g} mm vs {b.name} {b.thickness:g} mm"
    for k in range((m + 1) // 2):
        a, b = dielectrics[k], dielectrics[m - 1 - k]
        if _ply_key(a.plies) != _ply_key(b.plies[::-1]):
            return f"{a.name} {_ply_key(a.plies)} vs {b.name} {_ply_key(b.plies[::-1])}"
    return None


def _fill_limit(lib: mat.MaterialLibrary, material_id: str | None) -> float | None:
    m = lib.materials.get(material_id or "")
    limit = ((m or {}).get("limits") or {}).get("max_copper_fill_mm_per_ply")
    return float(limit) if is_number(limit) else None


def copper_fill_issues(copper: Sequence[Any], dielectrics: Sequence[Any],
                       ply_paths: Sequence[Sequence[Path]],
                       lib: mat.MaterialLibrary) -> list[Issue]:
    """ST-W4: a bondply ply fills only so much copper (the library's
    ``limits.max_copper_fill_mm_per_ply``, RO4450F 45.7 um). The first ply of a
    dielectric faces the copper above it, the last the copper below."""
    out: list[Issue] = []
    for k, die in enumerate(dielectrics):
        last = len(die.plies) - 1
        for j, ply in enumerate(die.plies):
            limit = _fill_limit(lib, ply.material_id)
            faces = [c for c, on in ((copper[k], j == 0), (copper[k + 1], j == last)) if on]
            need = sum(c.thickness for c in faces)
            if limit is None or need <= limit + 1e-9:
                continue
            out.append(warning("ST-W4", f"this {ply.label or ply.material_id} ply must fill "
                                        f"{need * 1000:.1f} um of copper "
                                        f"({' + '.join(c.name for c in faces)}) but fills at most "
                                        f"{limit * 1000:.1f} um per ply (Rogers RO4400 bondply "
                                        "data sheet): add a ply or use thinner copper",
                               ply_paths[k][j]))
    return out
