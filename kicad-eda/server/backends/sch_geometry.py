"""Pure schematic geometry for the API backend (no kipy, no KiCad).

Coordinates are millimetres on KiCad's sheet: +X right, +Y DOWN. Symbol pin
positions come from the API in the symbol's local frame with no rotation or
mirror applied (already y-down: at rotation 0 the sheet position is simply
instance + pin). ``transform_offset`` applies the instance transform.

The rotation table and the mirror axes were VERIFIED on 2026-09-17 against
KiCad 10.99.0-4025's own netlist: for each of 0/90/180/270 x {none, mirror_x,
mirror_y} a wire was drawn from the predicted pin position to a label, and
GetSchematicNetlist listed exactly that pin in the label's net. Two facts the
oracle also showed: KiCad rotates counter-clockwise on screen (which is
clockwise in y-down maths, hence 90 -> (dy, -dx)), and it normalises mirror
combinations (rotation 90 + mirror_y comes back as rotation 270 + mirror_x),
so callers must always transform with the instance's READ-BACK transform.
"""
from __future__ import annotations

import math

GRID_MM = 1.27                      # KiCad's default schematic connection grid
NM_PER_MM = 1_000_000
ROTATIONS = (0, 90, 180, 270)

# sch_build.py placement parity: one cell per (ref, unit), 5 columns
PLACEMENT_COLS = 5
PLACEMENT_PITCH_MM = (63.5, 50.8)
PLACEMENT_ORIGIN_MM = (50.8, 38.1)
FLAG_ORIGIN_MM = (363.22, 38.1)     # PWR_FLAG column on the right, 15.24 mm apart
FLAG_PITCH_MM = 15.24
STUB_MM = 5.08                      # short wire from a pin to a label / test point

Point = tuple[float, float]
Segment = tuple[float, float, float, float]


def orientation_deg(rot: float | int) -> int:
    """Normalise a rotation to one of 0/90/180/270 or raise ValueError."""
    try:
        value = float(rot)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"rotation must be a number, got {rot!r}") from exc
    if not math.isfinite(value) or value != int(value):
        raise ValueError(f"rotation must be one of {ROTATIONS}, got {rot!r}")
    deg = int(value) % 360
    if deg not in ROTATIONS:
        raise ValueError(f"rotation must be one of {ROTATIONS}, got {rot!r}")
    return deg


def snap(v_mm: float, grid: float = GRID_MM) -> float:
    """Nearest multiple of the connection grid, rounded to 4 decimals."""
    if not math.isfinite(float(v_mm)):
        raise ValueError(f"coordinate must be finite, got {v_mm!r}")
    return round(round(float(v_mm) / grid) * grid, 4)


def snap_point(x_mm: float, y_mm: float) -> Point:
    return snap(x_mm), snap(y_mm)


def to_mm(nm: int) -> float:
    return round(nm / NM_PER_MM, 4)


def to_nm(mm: float) -> int:
    return int(round(float(mm) * NM_PER_MM))


def rotate_offset(dx: float, dy: float, deg: int) -> tuple[float, float]:
    """Rotate a rotation-0 pin offset by the symbol's orientation (y-down)."""
    table = {0: (dx, dy), 90: (dy, -dx), 180: (-dx, -dy), 270: (-dy, dx)}
    if deg not in table:
        raise ValueError(f"rotation must be one of {ROTATIONS}, got {deg!r}")
    return table[deg]


def transform_offset(dx: float, dy: float, deg: int,
                     mirror_x: bool = False, mirror_y: bool = False) -> tuple[float, float]:
    """Rotate, then mirror: mirror_x flips the y axis, mirror_y flips the x
    axis (KiCad's naming: mirrored ABOUT that axis). Verified, see module doc."""
    rx, ry = rotate_offset(dx, dy, deg)
    if mirror_x:
        ry = -ry
    if mirror_y:
        rx = -rx
    return rx, ry


def pin_world_nm(inst_xy_nm: tuple[int, int], pin_xy_nm: tuple[int, int], deg: int,
                 mirror_x: bool = False, mirror_y: bool = False) -> tuple[int, int]:
    """Absolute pin position in nanometres (exact integer arithmetic)."""
    ox, oy = transform_offset(pin_xy_nm[0], pin_xy_nm[1], deg, mirror_x, mirror_y)
    return inst_xy_nm[0] + int(ox), inst_xy_nm[1] + int(oy)


def is_aligned(a: Point, b: Point, tol: float = 1e-6) -> bool:
    return abs(a[0] - b[0]) <= tol or abs(a[1] - b[1]) <= tol


def route(a: Point, b: Point) -> list[Segment]:
    """Grid-snapped wire segments from a to b: nothing for the same point, one
    straight wire when aligned, else horizontal-then-vertical (an L)."""
    ax, ay = snap_point(*a)
    bx, by = snap_point(*b)
    if abs(ax - bx) <= 1e-6 and abs(ay - by) <= 1e-6:
        return []
    if is_aligned((ax, ay), (bx, by)):
        return [(ax, ay, bx, by)]
    return [(ax, ay, bx, ay), (bx, ay, bx, by)]


def stub_from(pin: Point, offset: tuple[float, float], length: float = STUB_MM) -> Point:
    """A point `length` beyond a pin, continuing AWAY from the symbol body
    along the pin's dominant axis (offset = pin position - symbol position)."""
    ox, oy = offset
    if abs(ox) >= abs(oy):
        step = (math.copysign(length, ox) if ox else length, 0.0)
    else:
        step = (0.0, math.copysign(length, oy))
    return snap(pin[0] + step[0]), snap(pin[1] + step[1])


def grid_cell(index: int, cols: int = PLACEMENT_COLS,
              pitch: tuple[float, float] = PLACEMENT_PITCH_MM,
              origin: tuple[float, float] = PLACEMENT_ORIGIN_MM) -> Point:
    """Centre of the index-th placement cell (row-major), as sch_build lays them."""
    if index < 0:
        raise ValueError(f"cell index must be >= 0, got {index}")
    return (round(origin[0] + (index % cols) * pitch[0], 4),
            round(origin[1] + (index // cols) * pitch[1], 4))


def flag_position(index: int) -> Point:
    """Where the index-th PWR_FLAG goes (a column on the right, as sch_build)."""
    if index < 0:
        raise ValueError(f"flag index must be >= 0, got {index}")
    return FLAG_ORIGIN_MM[0], round(FLAG_ORIGIN_MM[1] + index * FLAG_PITCH_MM, 4)
