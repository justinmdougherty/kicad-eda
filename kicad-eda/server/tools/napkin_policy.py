#!/usr/bin/env python3
"""Hands-off (napkin) policies for the gates (ROADMAP D5).

Pure functions, stdlib only (Python 3.11-compatible, no KiCad): gates.py
turns the cases these policies allow into AUTO tasks, remediate.py applies
them, and the harness records one decision per applied remedy.

Floating pins (D5): auto ``no_connect`` only for output / bidirectional /
passive / NC-named pins on ICs and modules; input / power_in stay designer
tasks. A pin whose name looks like a supply or ground is NEVER no-connected:
a stacked / duplicate SUPPLY-named pin whose name equals a NETTED pin of the
same part (D1: the LG290P's 55 GND pads, stacked pins typed passive) joins
that net; any other supply-named pin stays a designer task. A NON-supply
name shared with a netted pin is a stacked pad of that net OR the same-named
pin of another unit / channel (an unused LM324 unit's '+' / '-', the AD620's
two 'Rg' gain-resistor pins): joining could tie an unused input to a live
net or short a part, no-connecting could orphan a stacked output pad, so it
stays a designer task (KiCad 9 stock-library scan, F5-08 rework).

G3 classifiers read the issue texts of swig_gate_checks.check_placement
(connector inward / far, fractured inner plane, short EP via farm); the
tests render those messages from the checker's SOURCE so a reworded message
fails loudly instead of silently disabling a policy.

Connector face-out geometry works on a swig_connector_probe entry
``{ref, mouth [mx, my], courtyard [x0, y0, x1, y1], centre, origin, rot,
side, pads}`` (mm, Y down). The probe reports the D2 board frame
(``board_bbox`` [0, 0, w, h]); face_out subtracts board_bbox x0 / y0, so a
sheet-frame entry with a sheet-frame bbox gives the same answer. Rotation
convention: the one swig_connector_probe documents and rotate_mouth there
implements (measured on KiCad 9.0.4 and 10.0.5; the tests compare both
functions): ``rot`` is the orientation KiCad DISPLAYS, and adding 90 degrees
maps the world mouth (mx, my) to (my, -mx) on both sides (right -> up ->
left -> down). The SWIG builder applies a component's netlist ``rot`` before
flipping a ``side: "B"`` part, and that flip displays ``-rot``; D3 ``at``
carries the displayed rotation.
"""
from __future__ import annotations

import math
import re
from typing import Any, Mapping

# --- policies and the fix kinds napkin mode emits -----------------------------------
POLICY_FLOATING_PIN = "D5.floating_pin"
POLICY_FACE_OUT = "D5.face_connector_out"
POLICY_FRACTURED_PLANE = "D5.fractured_plane"
POLICY_THERMAL_VIAS = "D5.thermal_vias"
NAPKIN_FIX_KINDS: tuple[str, ...] = ("declare_no_connect", "net_same_name_pin",
                                     "face_connector_out", "loosen_placement",
                                     "swap_thermal_footprint")
POLICY_FOR: Mapping[str, str] = {
    "declare_no_connect": POLICY_FLOATING_PIN,
    "net_same_name_pin": POLICY_FLOATING_PIN,
    "face_connector_out": POLICY_FACE_OUT,
    "loosen_placement": POLICY_FRACTURED_PLANE,
    "swap_thermal_footprint": POLICY_THERMAL_VIAS,
}

# --- floating pins ---------------------------------------------------------------------
NO_CONNECT_TYPES: tuple[str, ...] = ("output", "bidirectional", "passive")
NO_CONNECT_CONFIDENCE: Mapping[str, str] = {"output": "med", "bidirectional": "med",
                                            "passive": "low"}
NC_NAME_RE = re.compile(r"^(N[.]?C[.]?|DNC|NO_?CONNECT|RESERVED|RSVD)[0-9]*$", re.IGNORECASE)
IC_PREFIXES = frozenset({"U", "IC", "M", "MOD", "A"})
MODULE_LIBS: tuple[str, ...] = ("RF_Module", "Module", "MCU_Module")
# supply / ground names: generous on purpose — a false "supply" only keeps a
# designer task, a missed one could no-connect a power pin
_SUPPLY_RES = (
    re.compile(r"(GND|VSS|VDD|VCC|VEE)", re.IGNORECASE),
    re.compile(r"^(VBAT|VIN|VBUS|VCAP|VREF|VSYS|VCORE|VPP|VIO|PWR)", re.IGNORECASE),
    re.compile(r"^[+-]?\d+(?:[.]\d+)?V\d*$", re.IGNORECASE),  # 3V3, +5V, 1V8, +3.3V
    re.compile(r"^(V[+-]|0V|EARTH)$", re.IGNORECASE),
)

# --- G3 issue texts (swig_gate_checks.check_placement / check_plane_integrity /
# check_ep_via_farms); the tests render the real messages from that file's source
_INWARD_RE = re.compile(r"^(?P<ref>\S+): mating face points INTO the board interior")
_FAR_RE = re.compile(r"^(?P<ref>\S+): mating face is (?P<dist>\d+(?:\.\d+)?) mm from the "
                     r"board edge it faces")
_PLANE_RE = re.compile(r"^(?P<net>.+?): plane on (?P<layer>\S+) is fractured into "
                       r"separate islands")
_EP_RE = re.compile(r"^(?P<ref>\S+): dissipating exposed-pad part has only \d+ thermal "
                    r"via\(s\) under the EP")

# --- connector face-out ------------------------------------------------------------------
EDGE_NORMALS: Mapping[str, tuple[int, int]] = {"left": (-1, 0), "right": (1, 0),
                                               "top": (0, -1), "bottom": (0, 1)}
_EDGE_ORDER: tuple[str, ...] = ("left", "right", "top", "bottom")
HEADER_LIB_PREFIXES: tuple[str, ...] = ("Connector_PinHeader_", "Connector_PinSocket_",
                                        "Connector_IDC")
HEADER_PITCHES_MM: tuple[float, ...] = (2.54, 2.0, 1.27)
HEADER_PITCH_TOL_MM = 0.02
# a footprint name that says the part mates through the edge (right-angle / edge mount)
_EDGE_MARKER_RE = re.compile(r"(horizontal|angled|right[_-]?angle|edge)", re.IGNORECASE)

# --- thermal variants ------------------------------------------------------------------
STOCK_THERMAL_LIBS: tuple[str, ...] = ("Package_CSP", "Package_DFN_QFN", "Package_LGA",
                                       "Package_QFP", "Package_SO", "Package_SON",
                                       "Package_TO_SOT_SMD")
THERMAL_SUFFIXES: tuple[str, ...] = ("_ThermalVias", "_ThermalVias2")


class FaceOutError(ValueError):
    """A probe entry face_out cannot place (no mating direction, bad boxes)."""


# --- names and parts -----------------------------------------------------------------
def lib_of(lib_ref: Any) -> str:
    """The library nickname of 'Lib:Name' ('' when there is none)."""
    if not isinstance(lib_ref, str) or ":" not in lib_ref:
        return ""
    return lib_ref.partition(":")[0]


def name_of(lib_ref: Any) -> str:
    """The item name of 'Lib:Name' (the whole string when there is no colon)."""
    text = lib_ref if isinstance(lib_ref, str) else ""
    return text.partition(":")[2] if ":" in text else text


def ref_prefix(ref: Any) -> str:
    """The reference prefix: the characters before the first digit or '$',
    upper-cased ('U$3' -> 'U', Eagle style)."""
    text = ref if isinstance(ref, str) else ""
    match = re.match(r"[^0-9$]*", text)
    return match.group(0).upper() if match else ""


def is_ic_or_module(comp: Mapping) -> bool:
    """An IC or module: ref prefix U / IC / M / MOD / A (no library nickname
    needed, so importer nicknames count), or a lib_id / footprint library
    starting with RF_Module, Module or MCU_Module."""
    if ref_prefix(comp.get("ref")) in IC_PREFIXES:
        return True
    libs = (lib_of(comp.get("lib_id")), lib_of(comp.get("footprint")))
    return any(lib.startswith(MODULE_LIBS) for lib in libs if lib)


def is_nc_name(name: Any) -> bool:
    """NC / N.C. / DNC / NO_CONNECT / RESERVED / RSVD (+ digits), any case."""
    return isinstance(name, str) and bool(NC_NAME_RE.fullmatch(name))


def is_supply_name(name: Any) -> bool:
    """A name that looks like a supply or ground (GND, AGND, VSS*, VDD*,
    VCC*, VBAT, VIN, 3V3, 5V, V+, ...)."""
    if not isinstance(name, str) or not name:
        return False
    return any(rx.search(name) for rx in _SUPPLY_RES)


# --- floating pins ---------------------------------------------------------------------
def describe_pin(pin_type: Any, pin_name: Any) -> str:
    """"output pin 'PA3'" / "unnamed output pin" for task and remedy texts."""
    kind = f"{pin_type or 'untyped'} pin"
    return f"{kind} '{pin_name}'" if pin_name else f"unnamed {kind}"


def no_connect_verdict(pin_type: Any, pin_name: Any) -> tuple[str, str] | None:
    """(confidence, rationale) when D5 lets a floating IC/module pin be
    declared no_connect, else None. NC-named pins are high confidence."""
    if is_nc_name(pin_name):
        return "high", f"pin name '{pin_name}' marks it not connected (datasheet NC/RESERVED)"
    if pin_type not in NO_CONNECT_TYPES:
        return None
    rationale = {
        "output": "an unused output drives nothing and may be left open",
        "bidirectional": "an unused I/O may be left open (firmware should park it as an "
                         "output or enable a pull)",
        "passive": "an unused passive pin on an IC/module; confirm in the datasheet that "
                   "it may float",
    }[pin_type]
    return NO_CONNECT_CONFIDENCE[pin_type], rationale


def floating_pin_action(detail: Mapping, comp: Mapping) -> tuple:
    """The D5 action for one design_report floating_detail entry:
    ('net_same_name_pin', net) — a supply-named pin only |
    ('no_connect', confidence, rationale) | ('designer', reason)."""
    ref, pad = detail.get("ref"), detail.get("pad")
    pin = detail.get("pin") or f"{ref}.{pad}"
    if not is_ic_or_module(comp):
        return ("designer", f"{pin}: {ref} is not an IC or module, so its floating pins "
                            f"need a decision (net it or declare it no_connect)")
    name, ptype, same = detail.get("name"), detail.get("type"), detail.get("same_name_net")
    if is_nc_name(name):
        return ("no_connect", *no_connect_verdict(ptype, name))
    if is_supply_name(name):
        if same:
            return ("net_same_name_pin", same)
        return ("designer", f"{pin} ('{name}') looks like a supply/ground pin and no pin of "
                            f"the same name on {ref} is netted: net it to its rail")
    if same:
        return ("designer", f"{pin} ('{name}') shares its name with a pin of {ref} on net "
                            f"{same}: a stacked pad of that net or the same-named pin of "
                            f"another unit / channel; net it or declare it no_connect")
    verdict = no_connect_verdict(ptype, name)
    if verdict is not None:
        return ("no_connect", *verdict)
    return ("designer", f"{pin} is {ptype or 'untyped'}: an input / power pin must be "
                        f"netted (D5), with a recorded rationale")


# --- G3 classifiers --------------------------------------------------------------------
def classify_g3_issue(text: Any) -> dict | None:
    """The auto fix for a swig_gate_checks placement issue, or None:
    connector inward / far -> face_connector_out, fractured plane ->
    loosen_placement, short EP via farm -> swap_thermal_footprint."""
    if not isinstance(text, str):
        return None
    if m := _INWARD_RE.match(text):
        return {"type": "face_connector_out", "ref": m["ref"], "issue": "inward",
                "dist_mm": None}
    if m := _FAR_RE.match(text):
        return {"type": "face_connector_out", "ref": m["ref"], "issue": "far",
                "dist_mm": float(m["dist"])}
    if m := _PLANE_RE.match(text):
        return {"type": "loosen_placement",
                "reason": f"{m['net']} plane on {m['layer']} is fractured into separate "
                          f"islands"}
    if m := _EP_RE.match(text):
        return {"type": "swap_thermal_footprint", "ref": m["ref"]}
    return None


# --- connector face-out ----------------------------------------------------------------
def rotate_mouth(vec: tuple, degrees: float) -> tuple:
    """A world-frame vector after the footprint's displayed rotation grows by
    ``degrees`` (a multiple of 90): +90 maps (x, y) to (y, -x)."""
    x, y = vec
    for _ in range(int(round(degrees / 90.0)) % 4):
        x, y = y, -x
    return (x, y)


def _numbers(value: Any, count: int, what: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != count:
        raise FaceOutError(f"{what} must be {count} numbers, got {value!r}")
    try:
        nums = tuple(float(v) for v in value)
    except (TypeError, ValueError) as e:
        raise FaceOutError(f"{what} must be {count} numbers, got {value!r}") from e
    if not all(math.isfinite(v) for v in nums):
        raise FaceOutError(f"{what} must be finite numbers, got {value!r}")
    return nums


def _scalar(value: Any, what: str) -> float:
    """A finite number (None reads as 0); FaceOutError otherwise."""
    if value is None:
        return 0.0
    try:
        num = float(value)
    except (TypeError, ValueError) as e:
        raise FaceOutError(f"{what} must be a number, got {value!r}") from e
    if not math.isfinite(num):
        raise FaceOutError(f"{what} must be a finite number, got {value!r}")
    return num


def _box(value: Any, what: str) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = _numbers(value, 4, what)
    if x1 < x0 or y1 < y0:
        raise FaceOutError(f"{what} must be [x0, y0, x1, y1] with x0 <= x1 and y0 <= y1, "
                           f"got {value!r}")
    return x0, y0, x1, y1


def _axis(mouth: Any) -> tuple[int, int]:
    """The probed mouth snapped to its dominant axis."""
    try:
        mx, my = _numbers(mouth, 2, "mouth")
    except FaceOutError:
        mx = my = 0.0
    if mx == 0 and my == 0:
        raise FaceOutError(f"connector has no mating direction (probe mouth {mouth!r})")
    if abs(mx) >= abs(my):
        return (1 if mx > 0 else -1, 0)
    return (0, 1 if my > 0 else -1)


def nearest_edge(point: tuple, board_bbox: tuple) -> str:
    """'left' | 'right' | 'top' | 'bottom': the board edge nearest ``point``."""
    x, y = point
    bx0, by0, bx1, by1 = board_bbox
    dist = {"left": x - bx0, "right": bx1 - x, "top": y - by0, "bottom": by1 - y}
    return min(_EDGE_ORDER, key=lambda e: (dist[e], _EDGE_ORDER.index(e)))


def facing_edge(probe_entry: Mapping, board_bbox: Any) -> str:
    """The edge face_out turns ``probe_entry`` to (nearest its courtyard centre)."""
    x0, y0, x1, y1 = _box(probe_entry.get("courtyard"), "courtyard")
    return nearest_edge(((x0 + x1) / 2, (y0 + y1) / 2), _box(board_bbox, "board_bbox"))


def _quarter_turns_to(mouth: tuple, normal: tuple) -> int:
    return next(d for d in (0, 90, 180, 270) if rotate_mouth(mouth, d) == normal)


def _clamp_centre(c: float, half: float, lo: float, hi: float) -> float:
    if hi - lo < 2 * half:
        return (lo + hi) / 2
    return min(max(c, lo + half), hi - half)


def _flush_centre(edge: str, centre: tuple, size: tuple, board: tuple,
                  overhang: float) -> tuple[float, float]:
    """The courtyard centre flush with ``edge`` (``overhang`` mm past it),
    the tangential coordinate clamped so the courtyard stays on the board."""
    (cx, cy), (w, h), (bx0, by0, bx1, by1) = centre, size, board
    if edge in ("left", "right"):
        x = bx0 - overhang + w / 2 if edge == "left" else bx1 + overhang - w / 2
        return x, _clamp_centre(cy, h / 2, by0, by1)
    y = by0 - overhang + h / 2 if edge == "top" else by1 + overhang - h / 2
    return _clamp_centre(cx, w / 2, bx0, bx1), y


def _deg(value: float) -> float | int:
    """Degrees normalised to [0, 360), an int when whole."""
    d = round(value % 360.0, 3) % 360.0
    return int(d) if d == int(d) else d


def _mm(value: float) -> float:
    return round(value, 3) + 0.0                      # + 0.0: no "-0.0"


def face_out(probe_entry: Mapping, board_bbox: Any, comp: Mapping) -> dict:
    """Where ``comp`` must sit so its mouth faces OUT of the board edge
    nearest its courtyard centre: {'x', 'y', 'rot'} (courtyard-bbox centre,
    D3) or, for a component placed by ``at``, {'at': [x, y, rot]} (origin and
    displayed rotation), in the board frame. A board-internal header returns
    {'interior': True}. The courtyard sits flush with the edge (``overhang``
    mm past it); the tangential coordinate is kept, clamped inside the board.
    Raises FaceOutError for an entry it cannot place. Pure."""
    if is_board_internal_header(comp, probe_entry):
        return {"interior": True}
    mouth = _axis(probe_entry.get("mouth"))
    board = _box(board_bbox, "board_bbox")
    x0, y0, x1, y1 = _box(probe_entry.get("courtyard"), "courtyard")
    centre = ((x0 + x1) / 2, (y0 + y1) / 2)
    edge = nearest_edge(centre, board)
    turn = _quarter_turns_to(mouth, EDGE_NORMALS[edge])
    size = (x1 - x0, y1 - y0) if turn % 180 == 0 else (y1 - y0, x1 - x0)
    nx, ny = _flush_centre(edge, centre, size, board, _scalar(comp.get("overhang"), "overhang"))
    shown = _scalar(probe_entry.get("rot"), "rot") + turn
    if "at" in comp:
        ox, oy = _numbers(probe_entry.get("origin"), 2, "origin")
        vx, vy = rotate_mouth((ox - centre[0], oy - centre[1]), turn)
        return {"at": [_mm(nx + vx - board[0]), _mm(ny + vy - board[1]), _deg(shown)]}
    back = str(probe_entry.get("side") or comp.get("side") or "F").upper() == "B"
    return {"x": _mm(nx - board[0]), "y": _mm(ny - board[1]),
            "rot": _deg(-shown if back else shown)}


def _has_edge_marker(comp: Mapping) -> bool:
    return bool(comp.get("edge_row")) or bool(_EDGE_MARKER_RE.search(
        name_of(comp.get("footprint"))))


def _header_pitch(pitch: Any) -> bool:
    if not isinstance(pitch, (int, float)) or isinstance(pitch, bool):
        return False
    return any(abs(pitch - p) <= HEADER_PITCH_TOL_MM for p in HEADER_PITCHES_MM)


def header_by_pads(pads: Any, comp: Mapping) -> bool:
    """A pin header recognised from the probe's pads summary {count, tht,
    rows, pitch_mm}: >= 2 through-hole pads in 1 or 2 straight rows at 2.54,
    2.0 or 1.27 mm pitch, and no right-angle / edge marker on the part."""
    if not isinstance(pads, Mapping):
        return False
    count = pads.get("count")
    return (pads.get("tht") is True and isinstance(count, int) and count >= 2
            and pads.get("rows") in (1, 2) and _header_pitch(pads.get("pitch_mm"))
            and not _has_edge_marker(comp))


def is_board_internal_header(comp: Mapping, probe_entry: Mapping | None = None) -> bool:
    """A header the edge rule must not push to an edge: a stock header family
    (Connector_PinHeader_*, Connector_PinSocket_*, Connector_IDC) or, for any
    library (importer nicknames included), a header by its pads summary."""
    if lib_of(comp.get("footprint")).startswith(HEADER_LIB_PREFIXES):
        return True
    return header_by_pads((probe_entry or {}).get("pads"), comp)


# --- thermal variants ------------------------------------------------------------------
def thermal_variants(footprint: Any) -> list[str]:
    """Candidate 'Lib:Name_ThermalVias' ids for a stock package footprint, in
    preference order; [] for a non-stock nickname or a footprint that already
    is a ThermalVias variant."""
    lib, name = lib_of(footprint), name_of(footprint)
    if lib not in STOCK_THERMAL_LIBS or not name or "ThermalVias" in name:
        return []
    return [f"{lib}:{name}{suffix}" for suffix in THERMAL_SUFFIXES]
