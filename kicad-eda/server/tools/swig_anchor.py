#!/usr/bin/env python3
"""D3 ``at`` placement and footprint attributes for the SWIG board builder.

ROADMAP_DECISIONS D3: a component's ``"at": [x, y, rot]`` is the footprint
ORIGIN in the board frame (mm, Y down, origin = the outline bbox top-left)
and its final rotation as KiCad displays it; for ``side: "B"`` that is the
post-flip rotation, the value the saved board stores and kicad_import_pcb
reads back (normalised to [0, 360), so rotations compare modulo 360).

The placement recipe (verified on 9.0.4 and 10.0.5, test_swig_anchor_live):

  1. ``SetPosition(origin + at)``, rounded to the nanometre (FromMM truncates);
  2. ``board.Add(fp)``: a parentless ``Flip`` segfaults pcbnew;
  3. side B only: ``fp.Flip(fp.GetPosition(), FLIP_DIRECTION_TOP_BOTTOM)``
     (== ``Flip(c, True)`` on 9 and 10): layer B.Cu, the origin stays put,
     the children are mirrored in Y and the orientation becomes ``-rot``;
  4. ``SetOrientationDegrees(rot)`` explicitly, so the displayed rotation is
     exactly ``rot`` on both sides (pads: the library pad positions, mirrored
     in Y on B, rotated by ``rot`` about the origin).

Because the flip centre is the origin and step 4 sets the orientation
absolutely, the flip direction cannot change the result (KiCad's LEFT_RIGHT
flip is TOP_BOTTOM plus a 180-degree turn about the centre, which step 4
overrides; a mutation run with LEFT_RIGHT produced the identical board).

Attributes (D4 board_only, plus the netlist's ``dnp`` / ``in_bom``):
board_only -> board-only, excluded from the BOM and the position files;
dnp -> DNP and excluded from the position files; ``in_bom: false`` ->
excluded from the BOM. Flags are only ever turned ON (a library footprint's
own attributes stay). A setter this pcbnew lacks is a warning, never faked.

Runs under KiCad's bundled Python (9.0.4 locally, 10.0.5 in Docker): stdlib
only, Python 3.11 syntax. ``pcbnew`` is passed in or imported lazily inside a
function, so the pure part is unit-tested in the venv
(tests/test_swig_anchor.py); callers import kicad_quiet before pcbnew.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)

NM_PER_MM = 1_000_000
_INT32 = 2**31 - 1                  # pcbnew coordinates are 32-bit nanometres
SIDES = ("F", "B")

# netlist flag -> the FOOTPRINT setters it turns on, in call order
_SETTERS_FOR: dict[str, tuple[str, ...]] = {
    "board_only": ("SetBoardOnly", "SetExcludedFromBOM", "SetExcludedFromPosFiles"),
    "dnp": ("SetDNP", "SetExcludedFromPosFiles"),
    "no_bom": ("SetExcludedFromBOM",),
}
# setter -> the (attr ...) token it writes to the saved board
SETTER_TOKEN: dict[str, str] = {
    "SetBoardOnly": "board_only",
    "SetDNP": "dnp",
    "SetExcludedFromBOM": "exclude_from_bom",
    "SetExcludedFromPosFiles": "exclude_from_pos_files",
}
MANAGED_TOKENS: frozenset[str] = frozenset(SETTER_TOKEN.values())
_FLAG_KEYS = ("board_only", "dnp", "in_bom")


# ----------------------------------------------------------------------------
# Pure helpers (no pcbnew)
# ----------------------------------------------------------------------------
def _real(v: Any, what: str) -> float:
    """`v` as a finite float; bools and non-numbers raise ValueError."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{what} must be a number, got {v!r}")
    out = float(v)
    if not math.isfinite(out):
        raise ValueError(f"{what} must be finite, got {v!r}")
    return out


def normalize_rot(r: float) -> float:
    """A rotation in degrees as a float in [0, 360); -0.0 becomes 0.0."""
    out = _real(r, "rotation") % 360.0
    if out >= 360.0:                # a tiny negative r: -1e-14 % 360.0 == 360.0
        out = 0.0
    return out + 0.0                # -0.0 + 0.0 == 0.0


def same_rot(a: float, b: float, tol: float = 1e-6) -> bool:
    """True when rotations `a` and `b` (degrees) agree modulo 360 within `tol`."""
    d = normalize_rot(_real(a, "rotation") - _real(b, "rotation"))
    return min(d, 360.0 - d) <= tol


def parse_at(at: Any) -> tuple[float, float, float]:
    """The D3 ``at`` triple ``[x, y, rot]`` (mm, mm, degrees) as floats.
    Raises ValueError unless it is a list/tuple of three finite numbers."""
    if not isinstance(at, (list, tuple)) or len(at) != 3:
        raise ValueError(f"'at' must be [x, y, rot] (three numbers), got {at!r}")
    try:
        x, y, rot = (_real(v, name) for v, name in zip(at, ("x", "y", "rot"), strict=True))
    except ValueError as e:
        raise ValueError(f"'at' {at!r}: {e}") from e
    return x, y, rot


def norm_side(side: Any) -> str:
    """``"F"`` or ``"B"`` (case-insensitive; None = the default front side)."""
    if side is None:
        return "F"
    if isinstance(side, str) and side.upper() in SIDES:
        return side.upper()
    raise ValueError(f"side must be 'F' or 'B', got {side!r}")


def _flag(comp: Mapping[str, Any], key: str) -> bool | None:
    value = comp.get(key)
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"{comp.get('ref', '?')}: '{key}' must be a boolean, got {value!r}")
    return value


def attr_setters(comp: Mapping[str, Any]) -> tuple[str, ...]:
    """The FOOTPRINT setters (each to be called with True) a component's
    ``board_only`` / ``dnp`` / ``in_bom`` flags need, deduplicated, in order.
    Raises ValueError on a non-boolean flag."""
    flags = {key: _flag(comp, key) for key in _FLAG_KEYS}
    wanted = [name for key in ("board_only", "dnp") if flags[key] is True
              for name in _SETTERS_FOR[key]]
    if flags["in_bom"] is False:
        wanted += _SETTERS_FOR["no_bom"]
    return tuple(dict.fromkeys(wanted))


def attr_tokens(comp: Mapping[str, Any]) -> frozenset[str]:
    """The ``(attr ...)`` tokens apply_attrs writes for `comp` (a subset of
    MANAGED_TOKENS); a builder or test compares them with the saved board."""
    return frozenset(SETTER_TOKEN[name] for name in attr_setters(comp))


def _nm(mm: float, what: str) -> int:
    """Millimetres to pcbnew nanometres, ROUNDED (pcbnew.FromMM truncates)."""
    out = int(round(mm * NM_PER_MM))
    if abs(out) > _INT32:
        raise ValueError(f"{what} = {mm} mm is outside pcbnew's coordinate range")
    return out


# ----------------------------------------------------------------------------
# pcbnew side
# ----------------------------------------------------------------------------
def _pcbnew(mod: Any) -> Any:
    """`mod`, else the real pcbnew (kicad_quiet is already imported above)."""
    if mod is not None:
        return mod
    import pcbnew
    return pcbnew


def _ref(fp: Any, comp: Mapping[str, Any] | None = None) -> str:
    ref = (comp or {}).get("ref")
    if ref:
        return str(ref)
    get = getattr(fp, "GetReference", None)
    return str(get()) if get is not None else "footprint"


def place_at(board: Any, fp: Any, at: Any, side: Any, ox: float, oy: float,
             pcbnew: Any = None) -> None:
    """Place `fp` (not yet on any board) by its D3 ``at`` on `side`.

    ``ox``/``oy`` (mm) is where the board frame's origin sits on the sheet.
    Validates everything before the first pcbnew call (ValueError), then:
    SetPosition(origin + at) -> board.Add(fp) -> Flip about the origin when
    the side differs (B) -> SetOrientationDegrees(rot)."""
    x, y, rot = parse_at(at)
    want = norm_side(side)
    sx = _nm(_real(ox, "ox") + x, "ox + at x")
    sy = _nm(_real(oy, "oy") + y, "oy + at y")
    if fp.GetParent() is not None:
        raise ValueError(f"{_ref(fp)}: place_at adds the footprint to the board itself, "
                         "but it is already on a board")
    pcb = _pcbnew(pcbnew)
    fp.SetPosition(pcb.VECTOR2I(sx, sy))
    board.Add(fp)                                   # Flip needs the board parent
    if (want == "B") != bool(fp.IsFlipped()):
        fp.Flip(fp.GetPosition(), getattr(pcb, "FLIP_DIRECTION_TOP_BOTTOM", True))
    fp.SetOrientationDegrees(rot)


def _version(pcbnew: Any) -> str:
    try:
        return str(_pcbnew(pcbnew).Version())
    except (ImportError, AttributeError):  # no pcbnew (venv) / no Version(): still warn
        return "unknown version"


def apply_attrs(fp: Any, comp: Mapping[str, Any], pcbnew: Any = None) -> list[str]:
    """Turn on the board_only / DNP / BOM / position-file attributes `comp`
    asks for. Returns warnings, one per setter this pcbnew does not expose
    (nothing is faked). Raises ValueError on a non-boolean flag."""
    warnings: list[str] = []
    for name in attr_setters(comp):
        setter = getattr(fp, name, None)
        if setter is None:
            warnings.append(f"{_ref(fp, comp)}: this pcbnew ({_version(pcbnew)}) has no "
                            f"FOOTPRINT.{name}; '{SETTER_TOKEN[name]}' was not set")
            continue
        setter(True)
    return warnings


def read_back(fp: Any, ox: float, oy: float) -> tuple[float, float, float, str]:
    """(x, y, rot, side) of a placed footprint in the board frame: its origin
    (mm, rounded to the nm), the displayed rotation in [0, 360) and "F"/"B"."""
    pos = fp.GetPosition()
    x = round(pos.x / NM_PER_MM - _real(ox, "ox"), 6)
    y = round(pos.y / NM_PER_MM - _real(oy, "oy"), 6)
    rot = normalize_rot(round(float(fp.GetOrientationDegrees()), 6))
    return x + 0.0, y + 0.0, rot, "B" if fp.IsFlipped() else "F"
