"""Layer-count policy, wave 2a (ADR D7.11): the designer declares the copper
layer count and why; the pipeline records it, checks it against the floors and,
in napkin mode only, supplies a default when nothing is declared.

* ``features(design)``: what the policy reads (plain JSON data): napkin mode, the
  fab (``stackup.fab``, else the preset's fab, else ``layer_policy.fab``, else
  jlcpcb), whether a count is declared, the rationale / assumptions, impedance
  and Zdiff nets, RF nets (``freq_hz`` >= 300 MHz or an ``rf.antennas`` feed),
  the line kinds requested and the BGA packages (pitch, grid and ball rings from
  the KiCad footprint name; signal rows from the balls the netlist connects).
* ``floors(design, rs=None, fab=None)``: the D7.11 floors that apply. F1 an
  impedance target (or ``impedance_control.required``) needs the fab's smallest
  impedance-controlled count (JLC 4; ST-W3). F2 a stripline / dual stripline /
  embedded microstrip request needs an inner signal layer next to its reference
  planes: 6 with the default role templates; a declared role layout
  (``stackup.layers`` or preset ``roles``) is left to ST-E9. F3 a BGA with
  pitch <= 0.8 mm and >= 3 signal rows needs 4, pitch <= 0.5 mm 6 (heuristic,
  confidence med). F0 (count validity) is stackup_model's ST-E2.
* ``check_declared(design, rs=None)``: ST-W12 when napkin mode declares no count
  (the default is applied and recorded) or declares one without
  ``stackup.rationale`` or a ``copper_layers`` / ``stackup`` assumption with a
  rationale; outside napkin mode when nothing is declared (the 4-layer D7.2.4
  default is used). Plus one warning per floor above the declared count, named
  by the floor (F1 is skipped when stackup_model already reported its ST-W3).
* ``napkin_default(design, fab=None)``: napkin mode with no count declared only:
  4 layers raised to the floors; ``JLC04161H-3313`` when impedance/RF nets exist,
  else ``JLC04161H-7628``; 6 layers ``JLC06161H-2116``. Another fab uses its own
  default template, or keeps its name on the JLC geometry. Returns the count,
  preset, rationale, a decision draft (decisions.append_decision fields: stage
  requirements, confidence med, override ``stackup.preset``) and the netlist
  patch ``{copper_layers, stackup {preset, origin 'layer_policy', rationale, ...}}``
  as new dicts. ``NapkinDefault.apply(design)`` returns the patched netlist.

Pure: no file, network or clock access; never mutates its inputs. The shipped
fab presets are read once per process (``shipped_fabs``) unless a FabSet is
passed. Stdlib only, Python 3.11-compatible.
"""
from __future__ import annotations

import copy
import functools
import math
import re
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping, Sequence

import fab_presets as fp
import stackup_roles as roles
from stackup_roles import Issue

if TYPE_CHECKING:
    from stackup_model import ResolvedStackup

__all__ = ["Floor", "NapkinDefault", "check_declared", "count_declared", "features", "floors",
           "is_napkin", "napkin_default", "shipped_fabs", "stackup_shape"]

DEFAULT_FAB = "jlcpcb"
DEFAULT_COUNT = 4                 # the D7.2.4 default and the napkin base count
RF_MIN_HZ = 3e8
INNER_LINES = ("stripline", "dual_stripline", "embedded_microstrip")
AUTO_LINES = (None, "auto")
LEGACY_KEYS = frozenset({"h", "er", "t"})
POLICY_KEYS = ("preset", "origin", "rationale")
ASSUMPTION_TOPICS = ("copper_layers", "stackup")
F2_COUNT = 6                      # first default template with an inner signal layer (D7.2.4)
BGA_FINE_PITCH, BGA_ULTRA_PITCH, BGA_MIN_ROWS = 0.8, 0.5, 3
F3_FINE_COUNT, F3_ULTRA_COUNT = 4, 6
PITCH_EPS = 1e-6
SUBJECTS_SHOWN = 6
NAPKIN_PRESETS = MappingProxyType({DEFAULT_FAB: MappingProxyType({
    4: MappingProxyType({"plain": "JLC04161H-7628", "impedance": "JLC04161H-3313"}),
    6: MappingProxyType({"plain": "JLC06161H-2116", "impedance": "JLC06161H-2116"})})})
PRESET_NOTES = MappingProxyType({
    "generic:2L-1.6": "cheapest; no plane next to the routing layers, and impedance is "
                      "uncontrolled at JLC (50 ohm about 3 mm wide)",
    "jlcpcb:JLC04161H-7628": "the standard JLC 4-layer build; 0.2104 mm prepreg, 50 ohm "
                             "about 0.37 mm wide",
    "jlcpcb:JLC04161H-3313": "thin 0.0994 mm prepreg; 50 ohm about 0.175 mm wide (unmasked, "
                             "1 oz), matching 0402 and module pads",
    "jlcpcb:JLC06161H-2116": "6 layers SIG / GND / SIG / SIG / PWR / SIG: two inner routing "
                             "layers, more cost and lead time"})
ALTERNATIVES = ((2, "generic:2L-1.6"), (4, "jlcpcb:JLC04161H-7628"),
                (4, "jlcpcb:JLC04161H-3313"), (6, "jlcpcb:JLC06161H-2116"))
DRAFT_QUESTION = "How many copper layers, and which stackup? (napkin mode, nothing declared)"
DRAFT_SOURCE = "ROADMAP D7.11 napkin default (layer_policy.napkin_default)"
JEDEC_ROWS = "ABCDEFGHJKLMNPRTUVWY"          # BGA row letters (no I, O, Q, S, X, Z)
_PITCH = re.compile(r"_P(\d+(?:\.\d+)?)(?:x(\d+(?:\.\d+)?))?mm")
_LAYOUT = re.compile(r"Layout(\d+)x(\d+)")
_BALL_COUNT = re.compile(r"BGA-(\d+)")
_BALL = re.compile(r"^([A-HJ-NPRT-WY]{1,2})(\d{1,3})$")
_INNER = re.compile(r"^In\d{1,2}\.Cu$")
_SUPPLY = re.compile(r"^(\+|[ADP]?(GND|VSS|VCC|VDD)|VBUS|VBAT|VIN)", re.IGNORECASE)


@dataclass(frozen=True)
class Floor:
    """One D7.11 floor: the smallest copper count ``id`` (F1-F3) allows, the lint
    issue a shortfall becomes, and the nets or refs that trigger it."""
    id: str
    min_count: int
    severity: str
    reason: str
    issue_id: str
    confidence: str = "high"
    subjects: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "subjects": list(self.subjects)}


@dataclass(frozen=True)
class NapkinDefault:
    """The napkin layer default. ``decision_draft`` and ``patch`` are new dicts
    owned by the caller (decisions.append_decision fields; netlist keys)."""
    copper_layers: int
    preset: str
    rationale: str
    decision_draft: dict[str, Any]
    patch: dict[str, Any]
    floors: tuple[Floor, ...] = ()

    def apply(self, design: Mapping[str, Any]) -> dict[str, Any]:
        """A new netlist: ``design`` with (a copy of) the patch on top."""
        return {**design, **copy.deepcopy(self.patch)}


@functools.lru_cache(maxsize=1)
def shipped_fabs() -> fp.FabSet:
    """The shipped fab presets (server/data/fabs), read once per process."""
    return fp.load_fabs()


def _fabs(fabs: fp.FabSet | None) -> fp.FabSet:
    return fabs if fabs is not None else shipped_fabs()


def _require_design(design: Any) -> None:
    if not isinstance(design, Mapping):
        raise TypeError(f"design must be a netlist object, got {type(design).__name__}")


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _names(nets: Sequence[str]) -> str:
    shown = ", ".join(nets[:SUBJECTS_SHOWN])
    more = len(nets) - SUBJECTS_SHOWN
    return f"{shown} and {more} more" if more > 0 else shown


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------
def is_napkin(design: Mapping[str, Any]) -> bool:
    return design.get("mode") == "napkin"


def stackup_shape(stackup: Any) -> str:
    """none | legacy | preset | layers | model (neither preset nor layers) | bad."""
    if stackup is None:
        return "none"
    if not isinstance(stackup, Mapping):
        return "bad"
    keys = set(stackup)
    if keys & LEGACY_KEYS:
        return "legacy"
    return "preset" if "preset" in keys else "layers" if "layers" in keys else "model"


def count_declared(design: Mapping[str, Any]) -> bool:
    """True when ``copper_layers``, a preset, a layer list or the legacy shorthand
    fixes the count (a malformed stackup counts too: the lint reports it)."""
    shape = stackup_shape(design.get("stackup"))
    return design.get("copper_layers") is not None or shape in ("legacy", "preset", "layers",
                                                               "bad")


def _rationale(design: Mapping[str, Any]) -> str | None:
    st = design.get("stackup")
    value = st.get("rationale") if isinstance(st, Mapping) else None
    return value.strip() if _text(value) else None


def _assumption_ids(design: Mapping[str, Any]) -> list[str]:
    block = design.get("assumptions")
    return [str(a.get("id") or f"assumptions[{i}]")
            for i, a in enumerate(block if isinstance(block, list) else [])
            if isinstance(a, Mapping) and a.get("topic") in ASSUMPTION_TOPICS
            and _text(a.get("rationale"))]


def _fab_of(design: Mapping[str, Any]) -> str | None:
    st = design.get("stackup")
    if isinstance(st, Mapping) and "fab" in st:
        return st["fab"] if isinstance(st["fab"], str) else None
    if isinstance(st, Mapping) and isinstance(st.get("preset"), str):
        try:
            return fp.parse_preset(st["preset"])[0]
        except ValueError:
            pass
    policy = design.get("layer_policy")
    if isinstance(policy, Mapping) and _text(policy.get("fab")):
        return policy["fab"]
    return DEFAULT_FAB


# ---------------------------------------------------------------------------
# Nets and lines
# ---------------------------------------------------------------------------
def _specs(design: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    specs = design.get("net_specs")
    return [(n, s) for n, s in (specs.items() if isinstance(specs, Mapping) else ())
            if isinstance(s, Mapping)]


def _rf_nets(design: Mapping[str, Any]) -> list[str]:
    nets = [n for n, s in _specs(design)
            if roles.is_number(s.get("freq_hz")) and s["freq_hz"] >= RF_MIN_HZ]
    rf = design.get("rf")
    antennas = rf.get("antennas") if isinstance(rf, Mapping) else None
    for a in antennas if isinstance(antennas, list) else []:
        feed = a.get("feed_net") if isinstance(a, Mapping) else None
        if _text(feed) and feed not in nets:
            nets.append(feed)
    return nets


def _line_kinds(design: Mapping[str, Any]) -> dict[str, str]:
    """{net: line} for explicit lines, and 'stripline' for an auto line allowed on
    an inner layer (D7.7: auto on an inner layer is a stripline)."""
    kinds: dict[str, str] = {}
    for net, s in _specs(design):
        line, layers = s.get("line"), s.get("layers")
        inner = any(isinstance(x, str) and _INNER.match(x)
                    for x in (layers if isinstance(layers, list) else []))
        if isinstance(line, str) and line not in AUTO_LINES:
            kinds[net] = line
        elif line in AUTO_LINES and inner:
            kinds[net] = "stripline"
    return kinds


# ---------------------------------------------------------------------------
# BGA packages
# ---------------------------------------------------------------------------
def _is_bga(footprint: str) -> bool:
    lib, _, name = footprint.partition(":")
    return lib == "Package_BGA" or "BGA" in (name or lib).upper()


def _pitch(name: str) -> float | None:
    m = _PITCH.search(name)
    return min(float(g) for g in m.groups() if g) if m else None


def _grid(name: str) -> list[int] | None:
    m = _LAYOUT.search(name)
    return [int(m.group(1)), int(m.group(2))] if m else None


def _rings(grid: list[int] | None, name: str) -> int | None:
    """Ball rings from the outside in: half the shorter grid side, else from the
    ball count of a square full array."""
    if grid:
        return math.ceil(min(grid) / 2)
    m = _BALL_COUNT.search(name)
    return math.ceil(math.ceil(math.sqrt(int(m.group(1)))) / 2) if m else None


def _row_index(letters: str) -> int:
    if len(letters) == 1:
        return JEDEC_ROWS.index(letters)
    return len(JEDEC_ROWS) * (JEDEC_ROWS.index(letters[0]) + 1) + JEDEC_ROWS.index(letters[1])


def _balls(ref: Any, design: Mapping[str, Any]) -> list[tuple[int, int, str]]:
    """(row, column, net) of every ball of ``ref`` the netlist connects."""
    nets = design.get("nets")
    out: list[tuple[int, int, str]] = []
    for net, conns in (nets.items() if isinstance(nets, Mapping) else ()):
        for c in conns if isinstance(conns, list) else []:
            if isinstance(c, (list, tuple)) and len(c) >= 2 and c[0] == ref:
                m = _BALL.match(str(c[1]))
                if m:
                    out.append((_row_index(m.group(1)), int(m.group(2)) - 1, str(net)))
    return out


def _dims(balls: Sequence[tuple[int, int, str]], grid: list[int] | None) -> tuple[int, int]:
    """(rows, columns) of the array: KiCad's Layout<cols>x<rows>, turned when the
    connected balls say otherwise, never smaller than what they reach."""
    rows = max(b[0] for b in balls) + 1
    cols = max(b[1] for b in balls) + 1
    if grid is None:
        return rows, cols
    g_cols, g_rows = grid
    if rows > g_rows or cols > g_cols:
        g_cols, g_rows = g_rows, g_cols
    return max(rows, g_rows), max(cols, g_cols)


def _supply_nets(design: Mapping[str, Any]) -> set[str]:
    nets = design.get("nets")
    planes = design.get("planes")
    names = {str(n) for n in (nets if isinstance(nets, Mapping) else ()) if _SUPPLY.match(str(n))}
    return names | {str(v) for v in (planes.values() if isinstance(planes, Mapping) else ())}


def _signal_rows(ref: Any, design: Mapping[str, Any], grid: list[int] | None) -> int | None:
    """How deep the deepest signal (non-supply) ball sits, in rings from the
    edge; None when the netlist names none of the part's balls."""
    balls = _balls(ref, design)
    if not balls:
        return None
    rows, cols = _dims(balls, grid)
    supply = _supply_nets(design)
    depth = [min(r, c, rows - 1 - r, cols - 1 - c) for r, c, net in balls if net not in supply]
    return max(depth) + 1 if depth else 0


def _bga(design: Mapping[str, Any]) -> list[dict[str, Any]]:
    comps = design.get("components")
    out: list[dict[str, Any]] = []
    for c in comps if isinstance(comps, list) else []:
        footprint = c.get("footprint") if isinstance(c, Mapping) else None
        if not isinstance(footprint, str) or not _is_bga(footprint):
            continue
        name = footprint.partition(":")[2] or footprint
        grid = _grid(name)
        out.append({"ref": c.get("ref"), "footprint": footprint, "pitch_mm": _pitch(name),
                    "grid": grid, "rings": _rings(grid, name),
                    "signal_rows": _signal_rows(c.get("ref"), design, grid)})
    return out


def features(design: Mapping[str, Any]) -> dict[str, Any]:
    """The policy inputs of ``design`` as plain JSON data (module docstring)."""
    _require_design(design)
    st = design.get("stackup")
    control = st.get("impedance_control") if isinstance(st, Mapping) else None
    kinds = _line_kinds(design)
    return {"napkin": is_napkin(design), "fab": _fab_of(design),
            "copper_layers": design.get("copper_layers"), "stackup_shape": stackup_shape(st),
            "count_declared": count_declared(design), "rationale": _rationale(design),
            "assumptions": _assumption_ids(design),
            "impedance_nets": roles.impedance_nets(design),
            "diff_nets": [n for n, s in _specs(design) if s.get("diff") is not None],
            "impedance_required": isinstance(control, Mapping) and control.get("required") is True,
            "rf_nets": _rf_nets(design), "line_kinds": kinds,
            "inner_line_nets": [n for n, k in kinds.items() if k in INNER_LINES],
            "bga": _bga(design)}


# ---------------------------------------------------------------------------
# Floors
# ---------------------------------------------------------------------------
def _f1(feat: Mapping[str, Any], fab: str | None, fabs: fp.FabSet) -> Floor | None:
    nets = feat["impedance_nets"]
    if not (nets or feat["impedance_required"]) or not isinstance(fab, str) or fab not in fabs:
        return None
    count = fp.impedance_min_layers(fabs, fab)
    if count is None:
        return None
    name = fabs.fabs[fab].get("name", fab)
    what = f"impedance targets on {_names(nets)}" if nets else "impedance_control.required"
    return Floor("F1", count, "warning", (
        f"{what}: {name} controls impedance only from {count} copper layers; below that the "
        "impedance is uncontrolled (90 ohm on 2L 1.6 mm FR-4 needs lines about 3 mm wide)"),
        "ST-W3", "high", tuple(nets))


def _declared_roles(design: Mapping[str, Any]) -> bool:
    st = design.get("stackup")
    shape = stackup_shape(st)
    return shape == "layers" or (shape == "preset" and st.get("roles") is not None)


def _f2(feat: Mapping[str, Any], design: Mapping[str, Any]) -> Floor | None:
    nets = feat["inner_line_nets"]
    if not nets or _declared_roles(design):
        return None
    kinds = " / ".join(sorted({feat["line_kinds"][n] for n in nets}))
    return Floor("F2", F2_COUNT, "warning", (
        f"{kinds} requested on {_names(nets)}: that needs an inner signal layer next to its "
        "reference planes, one on each side for a stripline; with the default role templates "
        f"(ADR D7.2.4) the first is {F2_COUNT} layers, SIG / GND / SIG / SIG / PWR / SIG; or "
        "declare the role layout (stackup.layers or preset roles) and ST-E9 checks it"),
        "ST-W12", "high", tuple(nets))


def _bga_floor(b: Mapping[str, Any]) -> int | None:
    pitch = b["pitch_mm"]
    if pitch is None:
        return None
    if pitch <= BGA_ULTRA_PITCH + PITCH_EPS:
        return F3_ULTRA_COUNT
    rows = b["signal_rows"] if b["signal_rows"] is not None else b["rings"]
    if pitch <= BGA_FINE_PITCH + PITCH_EPS and (rows is None or rows >= BGA_MIN_ROWS):
        return F3_FINE_COUNT
    return None


def _bga_reason(b: Mapping[str, Any], count: int) -> str:
    head = f"{b['ref']} ({b['footprint']}) is a BGA at {b['pitch_mm']:g} mm pitch"
    if count == F3_ULTRA_COUNT:
        return (f"{head}: escape needs via-in-pad, taken as {count} layers (heuristic; JLC "
                "fills via-in-pad free from 6 layers)")
    if b["signal_rows"] is not None:
        rows = f"signal balls {b['signal_rows']} rings deep"
    else:
        rows = f"{b['rings']} ball rings (signal rows unknown)" if b["rings"] else "unknown rows"
    return (f"{head} with {rows}: about 2 rows escape per signal layer, so {count} layers "
            "(TI 0.8 mm BGA note)")


def _f3(feat: Mapping[str, Any]) -> Floor | None:
    hits = [(b, n) for b in feat["bga"] for n in (_bga_floor(b),) if n]
    if not hits:
        return None
    count = max(n for _, n in hits)
    return Floor("F3", count, "warning", "; ".join(_bga_reason(b, n) for b, n in hits),
                 "ST-W12", "med" if count >= F3_ULTRA_COUNT else "high",
                 tuple(str(b["ref"]) for b, _ in hits))


def _floors_of(feat: Mapping[str, Any], design: Mapping[str, Any], fab: str | None,
               fabs: fp.FabSet) -> tuple[Floor, ...]:
    found = (_f1(feat, fab, fabs), _f2(feat, design), _f3(feat))
    return tuple(f for f in found if f is not None)


def floors(design: Mapping[str, Any], rs: ResolvedStackup | None = None, fab: str | None = None,
           *, fabs: fp.FabSet | None = None) -> tuple[Floor, ...]:
    """The D7.11 floors F1-F3 that ``design`` triggers, in id order. ``fab`` None
    means the design's: ``rs.fab`` when resolved, else features' fab."""
    feat = features(design)
    chosen = fab if fab is not None else (rs.fab if rs is not None else feat["fab"])
    return _floors_of(feat, design, chosen, _fabs(fabs))


# ---------------------------------------------------------------------------
# check_declared: ST-W12 and floor shortfalls
# ---------------------------------------------------------------------------
def _rationale_path(feat: Mapping[str, Any]) -> tuple[str, ...]:
    shape = feat["stackup_shape"]
    if shape in ("preset", "layers", "model"):
        return ("stackup", "rationale")
    return ("stackup",) if shape == "legacy" else ("copper_layers",)


def _count_path(feat: Mapping[str, Any]) -> tuple[str, ...]:
    if feat["copper_layers"] is not None:
        return ("copper_layers",)
    return {"preset": ("stackup", "preset"), "layers": ("stackup", "layers"),
            "legacy": ("stackup",)}.get(feat["stackup_shape"], ("copper_layers",))


def _strict_default(fabs: fp.FabSet) -> str:
    try:
        return fp.default_template(fabs, DEFAULT_FAB, DEFAULT_COUNT)
    except LookupError:
        return "template"


def _w12(design: Mapping[str, Any], feat: Mapping[str, Any], fabs: fp.FabSet) -> list[Issue]:
    if feat["napkin"] and not feat["count_declared"]:
        nd = napkin_default(design, fabs=fabs)
        return [roles.warning("ST-W12", (
            "napkin mode and no layer count declared: the pipeline applies the ADR D7.11 "
            f"default ({nd.copper_layers}L {nd.preset}) and records the decision; declare "
            "copper_layers or a stackup with stackup.rationale to choose yourself"),
            ("copper_layers",))]
    if feat["napkin"] and not (feat["rationale"] or feat["assumptions"]):
        return [roles.warning("ST-W12", (
            "napkin mode: the layer count is declared without a rationale; add "
            "stackup.rationale (or an assumption with topic copper_layers or stackup and a "
            "rationale) saying why this count and build (ADR D7.11)"), _rationale_path(feat))]
    if feat["napkin"] or feat["count_declared"]:
        return []
    return [roles.warning("ST-W12", (
        f"no layer count declared: the {DEFAULT_COUNT}-layer default {_strict_default(fabs)} is "
        "used (ADR D7.2.4); declare copper_layers or a stackup and give stackup.rationale "
        "(ADR D7.11)"), ("copper_layers",))]


def _declared_count(rs: ResolvedStackup | None, feat: Mapping[str, Any]) -> int | None:
    if rs is not None:
        return rs.copper_layers
    if feat["copper_layers"] is not None:
        try:
            return roles.check_count(feat["copper_layers"])
        except ValueError:
            return None
    return None if feat["count_declared"] else DEFAULT_COUNT


def _reported_by_stackup(floor: Floor, rs: ResolvedStackup | None, fab: str | None) -> bool:
    """stackup_model already gives ST-W3 for impedance_control.required on a count
    the fab does not control: F1 would say it twice."""
    return floor.id == "F1" and rs is not None and rs.impedance_controlled and rs.fab == fab


def check_declared(design: Mapping[str, Any], rs: ResolvedStackup | None = None, *,
                   fabs: fp.FabSet | None = None) -> tuple[Issue, ...]:
    """G1 issues of the declared layer count (module docstring): ST-W12 and one
    warning per floor above the count (ST-W3 for F1, ST-W12 for F2/F3)."""
    feat = features(design)
    use = _fabs(fabs)
    issues = _w12(design, feat, use)
    count = _declared_count(rs, feat)
    if (feat["napkin"] and not feat["count_declared"]) or count is None:
        return tuple(issues)                 # the napkin default raises to the floors itself
    fab = rs.fab if rs is not None else feat["fab"]
    for floor in _floors_of(feat, design, fab, use):
        if floor.min_count <= count or _reported_by_stackup(floor, rs, fab):
            continue
        issues.append(roles.warning(floor.issue_id, (
            f"floor {floor.id}: {count} copper layer{'s' if count != 1 else ''} is below its "
            f"minimum of {floor.min_count}: {floor.reason}"), _count_path(feat)))
    return tuple(issues)


# ---------------------------------------------------------------------------
# napkin_default
# ---------------------------------------------------------------------------
def _preset(count: int, fab: str | None, impedance: bool,
            fabs: fp.FabSet) -> tuple[str, str | None, str | None]:
    """(preset, the fab to keep on the stackup or None, a note for the rationale)."""
    key = "impedance" if impedance else "plain"
    table = NAPKIN_PRESETS.get(fab or DEFAULT_FAB)
    if table is not None and count in table:
        return f"{fab or DEFAULT_FAB}:{table[count][key]}", None, None
    if fab is not None and fab in fabs:
        try:
            return fp.default_template(fabs, fab, count), None, None
        except LookupError:
            pass
    jlc = NAPKIN_PRESETS[DEFAULT_FAB].get(count)
    preset = (f"{DEFAULT_FAB}:{jlc[key]}" if jlc is not None
              else fp.default_template(fabs, DEFAULT_FAB, count))
    if fab is None:
        return preset, None, None
    if fab not in fabs:
        return preset, None, f"unknown fab {fab!r} ignored: the JLC geometry is used"
    return preset, fab, (f"{fab} publishes no {count}-layer template: the JLC geometry is used "
                         f"with stackup.fab {fab}; confirm the build with {fab}")


def _why_preset(preset: str, feat: Mapping[str, Any], impedance: bool) -> str:
    nets = list(dict.fromkeys(feat["impedance_nets"] + feat["rf_nets"]))
    why = (f"impedance/RF nets exist ({_names(nets)})" if nets
           else "impedance_control.required" if impedance else "no impedance or RF nets")
    note = PRESET_NOTES.get(preset)
    return f"{preset} because {why}" + (f"; {note}" if note else "")


def _rationale_text(count: int, preset: str, found: Sequence[Floor], feat: Mapping[str, Any],
                    impedance: bool, notes: Sequence[str | None]) -> str:
    raising = [f for f in found if f.min_count > DEFAULT_COUNT]
    head = f"Napkin default (ADR D7.11): {count} copper layers"
    if raising:
        head += f", raised from {DEFAULT_COUNT} by " + "; ".join(
            f"{f.id} ({f.reason})" for f in raising)
    else:
        head += (", the usual default (easier to route than 2 layers, a solid GND plane next "
                 "to the outer layers)")
    parts = [head, _why_preset(preset, feat, impedance)]
    met = [f for f in found if f not in raising]
    if met:
        parts.append("Floors met: " + "; ".join(f"{f.id} {f.min_count} ({f.reason})"
                                                for f in met))
    parts += [n for n in notes if n]
    return ". ".join(parts) + "."


def _stackup_patch(design: Mapping[str, Any], preset: str, rationale: str,
                   keep_fab: str | None) -> dict[str, Any]:
    st = design.get("stackup")
    base = ({k: copy.deepcopy(v) for k, v in st.items() if k not in POLICY_KEYS}
            if isinstance(st, Mapping) else {})
    out = {"preset": preset, "origin": "layer_policy", "rationale": rationale, **base}
    if keep_fab is not None and "fab" not in base:
        out["fab"] = keep_fab
    return out


def _alternatives(preset: str, found: Sequence[Floor]) -> list[str]:
    out = []
    for count, alt in ALTERNATIVES:
        if alt == preset:
            continue
        below = [f.id for f in found if f.min_count > count]
        tail = f" (below floor {', '.join(below)})" if below else ""
        out.append(f"{count}L {alt}: {PRESET_NOTES[alt]}{tail}")
    return out


def _draft(count: int, preset: str, rationale: str, found: Sequence[Floor]) -> dict[str, Any]:
    return {"stage": "requirements", "question": DRAFT_QUESTION, "decision": f"{count}L {preset}",
            "rationale": rationale, "alternatives": _alternatives(preset, found),
            "confidence": "med", "source": DRAFT_SOURCE, "reversible": True,
            "override": "stackup.preset"}


def napkin_default(design: Mapping[str, Any], fab: str | None = None, *,
                   fabs: fp.FabSet | None = None) -> NapkinDefault | None:
    """The D7.11 napkin default, or None outside napkin mode or when a count is
    declared. ``fab`` None means the design's (features). Never mutates ``design``."""
    _require_design(design)
    if not is_napkin(design) or count_declared(design):
        return None
    use = _fabs(fabs)
    feat = features(design)
    chosen = fab if fab is not None else feat["fab"]
    found = _floors_of(feat, design, chosen, use)
    count = max([DEFAULT_COUNT, *(f.min_count for f in found)])
    count += count % 2                                   # multilayer counts are even
    impedance = bool(feat["impedance_nets"] or feat["rf_nets"] or feat["impedance_required"])
    preset, keep_fab, note = _preset(count, chosen, impedance, use)
    designer = f"Designer note: {feat['rationale'].rstrip('.')}" if feat["rationale"] else None
    rationale = _rationale_text(count, preset, found, feat, impedance, (note, designer))
    patch = {"copper_layers": count,
             "stackup": _stackup_patch(design, preset, rationale, keep_fab)}
    return NapkinDefault(count, preset, rationale, _draft(count, preset, rationale, found),
                         patch, found)
