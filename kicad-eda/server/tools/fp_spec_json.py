#!/usr/bin/env python3
"""JSON-shaped params -> a FootprintSpec from an fp_author_templates template
(F2, ROADMAP D1).

``spec_from_json(template, params)`` is the boundary an MCP tool or a netlist
hands footprint geometry through: ``template`` names one of TEMPLATES and
``params`` is a JSON object (a Mapping, or JSON text) of that template's
keyword arguments, nested dataclasses written as objects:

  body       {"w", "h", "x"?, "y"?, "w_max"?, "h_max"?, "height"?}
  ep         {"w", "h", "number"?, "x"?, "y"?, "paste_grid"? [nx, ny],
              "paste_coverage"?, "vias"? {"nx", "ny", "pitch", "drill"?, "size"?,
              "bottom_pad"?}}
  edge_rows  [{"side", "at", "positions", "first", "length", "width", "shape"?}]
  grid       {"xs", "ys", "w", "h", "shape"?}
  numbering  {"rule": "linear", "order": "col_major"|"row_major", "start"?, "skip"?}
             {"rule": "grid_ref", "skip"?}       row letters (JEDEC: no I O Q S X Z) +
                                                column number from 1: "A1", "B3", "AA1"
             {"rule": "table", "names": [[name|null, ...], ...]}   names[row][col]
  model      {"path", "offset"? [x, y, z], "scale"? [...], "rotate"? [...]}

``positions`` / ``xs`` / ``ys`` are coordinate runs: an array whose items are
numbers or run objects, or one run object; a run object is
``{"count", "pitch", "start"}`` (start, start + pitch, ...) or
``{"count", "pitch", "centre"?}`` (centred on centre, default 0). A grid
numbering is positional: a ``skip`` cell [col, row] has no pad and its number
is not reused. Every template also takes the FootprintSpec keywords descr,
tags, datasheet, model, pin1 (null = no pin-1 marker), pin1_side, attr_flags,
courtyard_excess; ``lg290p`` takes only those (its geometry is fixed). Pad
names may be JSON integers (written as strings).

Every problem is collected and raised at once as FpSpecJsonError (a
ValueError) whose ``problems`` name the JSON path, e.g. ``sop: params.body:
unknown key 'hieght' (did you mean 'height'?); allowed: h, height, ...``.
Template errors (an odd SOP pin count, a bad pad shape) are wrapped the same
way. Size limit: no count that multiplies into pads (run counts, pin counts,
rows / cols, via and paste grids) and no coordinate run may exceed MAX_PADS,
and a footprint whose pad entries (copper, paste windows, vias) could exceed
it is refused before anything is built, so a typo such as ``"count": 1e8``
cannot stall the server. Pad numbers (``start``, ``first``) are not sizes.
The input is only read, never mutated. Stdlib-only and Python 3.11
compatible: KiCad's bundled Python can import it.
"""
from __future__ import annotations

import difflib
import json
import math
from typing import Any, Callable, Mapping

import fp_author_templates as ft
from fp_author_templates import FootprintSpec

TEMPLATES: tuple[str, ...] = ("lga_grid", "qfn", "dfn", "lcc_castellated", "sop", "pin_header",
                              "lg290p")
NUMBERING_RULES: tuple[str, ...] = ("grid_ref", "linear", "table")
ORDERS: tuple[str, ...] = ("col_major", "row_major")
JEDEC_ROW_LETTERS = "ABCDEFGHJKLMNPRTUVWY"     # JEDEC JEP95 / IPC-7351: no I O Q S X Z
MAX_PADS = 10_000                              # pad entries per footprint (largest BGAs ~3,000)

Problems = list[str]
Converter = Callable[[Any, str, Problems], Any]
Field = tuple[Converter, bool]                 # (converter, required)


class FpSpecJsonError(ValueError):
    """Template params that do not describe a footprint; ``problems`` lists
    every reason, each naming its JSON path."""

    def __init__(self, problems: list[str] | tuple[str, ...]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems))


class _Invalid:
    """The value of a JSON node that failed conversion (its problem is recorded)."""


_BAD = _Invalid()


# ---------------------------------------------------------------------------
# Scalars
# ---------------------------------------------------------------------------
def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return type(value).__name__


def _shown(value: Any) -> str:
    """``value`` as an error message shows it: its JSON type and, for scalars, the value."""
    kind = _json_type(value)
    if isinstance(value, str):
        return f"{kind} {value!r}"
    if isinstance(value, bool) or kind == "number":
        return f"{kind} {json.dumps(value)}"
    return kind                                  # null, object, array: the type says it all


def _fail(problems: Problems, path: str, expected: str, value: Any) -> _Invalid:
    problems.append(f"{path}: expected {expected}, got {_shown(value)}")
    return _BAD


def _is_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _number(value: Any, path: str, problems: Problems) -> float | _Invalid:
    return float(value) if _is_number(value) else _fail(problems, path, "a number", value)


def _positive(value: Any, path: str, problems: Problems) -> float | _Invalid:
    if not _is_number(value):
        return _fail(problems, path, "a number", value)
    if value <= 0:
        problems.append(f"{path}: must be > 0, got {json.dumps(value)}")
        return _BAD
    return float(value)


def _count(value: Any, path: str, problems: Problems) -> int | _Invalid:
    """A non-negative integer (an integral JSON float such as 4.0 is accepted)."""
    if not _is_number(value) or (isinstance(value, float) and not value.is_integer()):
        return _fail(problems, path, "an integer", value)
    if value < 0:
        problems.append(f"{path}: must be >= 0, got {json.dumps(value)}")
        return _BAD
    return int(value)


def _size(value: Any, path: str, problems: Problems) -> int | _Invalid:
    """A count that multiplies into pads: an integer 0..MAX_PADS."""
    n = _count(value, path, problems)
    if n is not _BAD and n > MAX_PADS:
        problems.append(f"{path}: must be <= {MAX_PADS}, got {n}")
        return _BAD
    return n


def _string(value: Any, path: str, problems: Problems) -> str | _Invalid:
    return value if isinstance(value, str) else _fail(problems, path, "a string", value)


def _boolean(value: Any, path: str, problems: Problems) -> bool | _Invalid:
    return value if isinstance(value, bool) else _fail(problems, path, "true or false", value)


def _pad_name(value: Any, path: str, problems: Problems) -> str | _Invalid:
    """A pad name: a string, or a JSON integer written as its decimal string."""
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return _fail(problems, path, "a pad name (string or integer)", value)


def _optional_pad_name(value: Any, path: str, problems: Problems) -> str | None | _Invalid:
    return None if value is None else _pad_name(value, path, problems)


# ---------------------------------------------------------------------------
# Arrays and objects
# ---------------------------------------------------------------------------
def _array(value: Any, path: str, problems: Problems, item: Converter,
           size: int | None = None) -> tuple | _Invalid:
    """A JSON array converted item by item (exactly ``size`` items when given)."""
    if not isinstance(value, (list, tuple)):
        return _fail(problems, path, "an array", value)
    if size is not None and len(value) != size:
        problems.append(f"{path}: expected an array of {size}, got {len(value)} item(s)")
        return _BAD
    start = len(problems)
    out = tuple(item(v, f"{path}[{i}]", problems) for i, v in enumerate(value))
    return out if len(problems) == start else _BAD


def _vec3(value: Any, path: str, problems: Problems) -> tuple | _Invalid:
    return _array(value, path, problems, _number, 3)


def _strings(value: Any, path: str, problems: Problems) -> tuple | _Invalid:
    return _array(value, path, problems, _string)


def _unknown_key(path: str, key: str, allowed: Mapping[str, Any] | tuple[str, ...]) -> str:
    near = difflib.get_close_matches(key, list(allowed), n=1)
    hint = f" (did you mean {near[0]!r}?)" if near else ""
    return f"{path}: unknown key {key!r}{hint}; allowed: {', '.join(sorted(allowed))}"


def _object(value: Any, path: str, problems: Problems,
            fields: Mapping[str, Field]) -> dict | _Invalid:
    """A JSON object checked against ``fields``: unknown keys (with the closest
    allowed key), missing required keys, then every value converted."""
    if not isinstance(value, Mapping):
        return _fail(problems, path, "an object", value)
    start = len(problems)
    problems.extend(_unknown_key(path, str(k), fields) for k in value if k not in fields)
    missing = [k for k, (_, required) in fields.items() if required and k not in value]
    if missing:
        problems.append(f"{path}: missing required key(s) {', '.join(missing)}")
    out = {k: conv(value[k], f"{path}.{k}", problems)
           for k, (conv, _) in fields.items() if k in value}
    return out if len(problems) == start else _BAD


def _built(cls: Callable[..., Any], fields: Mapping[str, Field]) -> Converter:
    """A converter for a JSON object that becomes ``cls(**object)``; the
    class's own validation errors are recorded at the object's path."""
    def convert(value: Any, path: str, problems: Problems) -> Any:
        kwargs = _object(value, path, problems, fields)
        if kwargs is _BAD:
            return _BAD
        try:
            return cls(**kwargs)
        except (TypeError, ValueError) as e:
            problems.append(f"{path}: {e}")
            return _BAD
    return convert


def _pad_shape_map(value: Any, path: str, problems: Problems) -> dict | _Invalid:
    """{pad name: shape}: per-pad shape overrides (lga_grid ``shapes``)."""
    if not isinstance(value, Mapping):
        return _fail(problems, path, "an object", value)
    start = len(problems)
    out = {str(k): _string(v, f"{path}.{k}", problems) for k, v in value.items()}
    return out if len(problems) == start else _BAD


# ---------------------------------------------------------------------------
# Coordinate runs
# ---------------------------------------------------------------------------
_RUN_FIELDS: dict[str, Field] = {"count": (_size, True), "pitch": (_number, True),
                                 "start": (_number, False), "centre": (_number, False)}


def _run_object(value: Any, path: str, problems: Problems) -> tuple | _Invalid:
    run = _object(value, path, problems, _RUN_FIELDS)
    if run is _BAD:
        return _BAD
    if "start" in run and "centre" in run:
        problems.append(f"{path}: give 'start' or 'centre', not both")
        return _BAD
    if run["count"] < 1:
        problems.append(f"{path}.count: must be >= 1, got {run['count']}")
        return _BAD
    if "start" in run:
        return ft.pitch_run(run["start"], run["pitch"], run["count"])
    return ft.centred_run(run["count"], run["pitch"], run.get("centre", 0.0))


def _run(value: Any, path: str, problems: Problems) -> tuple[float, ...] | _Invalid:
    """Coordinates: a run object, or a non-empty array of numbers and run objects."""
    if isinstance(value, Mapping):
        return _run_object(value, path, problems)
    if not isinstance(value, (list, tuple)) or not value:
        return _fail(problems, path, "a non-empty array or a run object", value)
    start, out = len(problems), []
    for i, item in enumerate(value):
        convert = _run_object if isinstance(item, Mapping) else _number
        part = convert(item, f"{path}[{i}]", problems)
        if part is not _BAD:
            out.extend(part if isinstance(part, tuple) else (part,))
        if len(out) > MAX_PADS:
            problems.append(f"{path}: more than {MAX_PADS} coordinates")
            return _BAD
    return tuple(out) if len(problems) == start else _BAD


# ---------------------------------------------------------------------------
# Grid numbering
# ---------------------------------------------------------------------------
def grid_ref(col: int, row: int) -> str:
    """JEDEC grid reference: row letters (A..Y without I O Q S X Z, then AA,
    AB, ... YY: 420 rows) and the column number from 1. ``grid_ref(0, 0) == "A1"``."""
    n = len(JEDEC_ROW_LETTERS)
    if not 0 <= row < n * (n + 1) or col < 0:
        raise ValueError(f"grid_ref: cell (col {col}, row {row}) is outside the lettered "
                         f"range (col >= 0, row 0..{n * (n + 1) - 1})")
    letters = JEDEC_ROW_LETTERS[row] if row < n else (
        JEDEC_ROW_LETTERS[row // n - 1] + JEDEC_ROW_LETTERS[row % n])
    return f"{letters}{col + 1}"


def _cell(value: Any, path: str, problems: Problems) -> tuple[int, int] | _Invalid:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return _fail(problems, path, "[col, row]", value)
    cell = _array(value, path, problems, _count, 2)
    return cell if cell is _BAD else (cell[0], cell[1])


def _table_row(value: Any, path: str, problems: Problems) -> tuple | _Invalid:
    return _array(value, path, problems, _optional_pad_name)


_SKIP: Field = (lambda v, p, pr: _array(v, p, pr, _cell), False)
_NUMBERING_FIELDS: dict[str, dict[str, Field]] = {
    "linear": {"rule": (_string, True), "order": (_string, True), "start": (_count, False),
               "skip": _SKIP},
    "grid_ref": {"rule": (_string, True), "skip": _SKIP},
    "table": {"rule": (_string, True),
              "names": (lambda v, p, pr: _array(v, p, pr, _table_row), True)},
}


def _numbering(value: Any, path: str, problems: Problems) -> dict | _Invalid:
    """The numbering object, checked against its rule's keys (the grid size
    is checked later, in _numbering_problems)."""
    if not isinstance(value, Mapping):
        return _fail(problems, path, "an object", value)
    if "rule" not in value:
        problems.append(f"{path}: missing required key(s) rule")
        return _BAD
    rule = value["rule"]
    if not isinstance(rule, str) or rule not in _NUMBERING_FIELDS:
        problems.append(f"{path}.rule: {rule!r} is not one of {', '.join(NUMBERING_RULES)}")
        return _BAD
    out = _object(value, path, problems, _NUMBERING_FIELDS[rule])
    if out is not _BAD and rule == "linear" and out["order"] not in ORDERS:
        problems.append(f"{path}.order: {out['order']!r} is not one of {', '.join(ORDERS)}")
        return _BAD
    return out


def _numbering_problems(spec: Mapping[str, Any], cols: int, rows: int, path: str) -> list[str]:
    """Skip cells outside the grid; a table whose size is not the grid's."""
    out = [f"{path}.skip[{i}]: {list(cell)} is outside the {cols} x {rows} grid (col, row)"
           for i, cell in enumerate(spec.get("skip", ()))
           if not (cell[0] < cols and cell[1] < rows)]
    names = spec.get("names")
    if names is not None and (len(names) != rows or any(len(r) != cols for r in names)):
        width = len(names[0]) if names else 0
        out.append(f"{path}.names: {width} x {len(names)} table does not match the "
                   f"{cols} x {rows} grid (columns x rows)")
    return out


def _numbering_fn(spec: Mapping[str, Any], cols: int,
                  rows: int) -> Callable[[int, int], str | None]:
    """The numbering(col, row) callable lga_grid takes."""
    skip = frozenset(spec.get("skip", ()))
    rule = spec["rule"]
    if rule == "table":
        names = spec["names"]
        return lambda c, r: None if (c, r) in skip else names[r][c]
    if rule == "grid_ref":
        return lambda c, r: None if (c, r) in skip else grid_ref(c, r)
    first, col_major = spec.get("start", 1), spec["order"] == "col_major"

    def linear(c: int, r: int) -> str | None:
        if (c, r) in skip:
            return None
        return str(first + (c * rows + r if col_major else r * cols + c))
    return linear


# ---------------------------------------------------------------------------
# Nested objects and the per-template schemas
# ---------------------------------------------------------------------------
_body = _built(ft.Body, {"w": (_positive, True), "h": (_positive, True), "x": (_number, False),
                         "y": (_number, False), "w_max": (_positive, False),
                         "h_max": (_positive, False), "height": (_positive, False)})
_model = _built(ft.Model, {"path": (_string, True), "offset": (_vec3, False),
                           "scale": (_vec3, False), "rotate": (_vec3, False)})
_vias = _built(ft.ThermalVias, {"nx": (_size, True), "ny": (_size, True),
                                "pitch": (_positive, True), "drill": (_positive, False),
                                "size": (_positive, False), "bottom_pad": (_boolean, False)})
_ep = _built(ft.ExposedPad, {
    "w": (_positive, True), "h": (_positive, True), "number": (_pad_name, False),
    "x": (_number, False), "y": (_number, False),
    "paste_grid": (lambda v, p, pr: _array(v, p, pr, _size, 2), False),
    "paste_coverage": (_number, False), "vias": (_vias, False)})
_edge_row = _built(ft.EdgeRow, {
    "side": (_string, True), "at": (_number, True), "positions": (_run, True),
    "first": (_count, True), "length": (_positive, True), "width": (_positive, True),
    "shape": (_string, False)})
_grid = _built(ft.PadGrid, {"xs": (_run, True), "ys": (_run, True), "w": (_positive, True),
                            "h": (_positive, True), "shape": (_string, False)})

_COMMON: dict[str, Field] = {
    "descr": (_string, False), "tags": (_string, False), "datasheet": (_string, False),
    "model": (_model, False), "pin1": (_optional_pad_name, False),
    "pin1_side": (_string, False), "attr_flags": (_strings, False),
    "courtyard_excess": (_number, False)}
_PACKAGE: dict[str, Field] = {"name": (_string, True), "body": (_body, True),
                              "pitch": (_positive, True), "pad_w": (_positive, True),
                              "pad_l": (_positive, True), "ep": (_ep, False),
                              "shape": (_string, False)}
_SCHEMAS: dict[str, dict[str, Field]] = {
    "lga_grid": {"name": (_string, True), "body": (_body, True),
                 "edge_rows": (lambda v, p, pr: _array(v, p, pr, _edge_row), False),
                 "grid": (_grid, False), "numbering": (_numbering, False),
                 "shapes": (_pad_shape_map, False)},
    "qfn": {**_PACKAGE, "pins_x": (_size, True), "pins_y": (_size, True),
            "span_x": (_positive, True), "span_y": (_positive, True)},
    "dfn": {**_PACKAGE, "pins_per_side": (_size, True), "span": (_positive, True)},
    "lcc_castellated": {**_PACKAGE,
                        "pins": (lambda v, p, pr: _array(v, p, pr, _size, 4), True),
                        "edge_offset": (_number, False)},
    "sop": {**_PACKAGE, "pins": (_size, True), "span": (_positive, True)},
    "pin_header": {"name": (_string, True), "rows": (_size, True), "cols": (_size, False),
                   "pitch": (_positive, False), "drill": (_positive, False),
                   "pad": (_positive, False)},
    "lg290p": {},
}


def _lga_grid(kwargs: Mapping[str, Any], problems: Problems) -> FootprintSpec:
    """lga_grid with its JSON numbering turned into the numbering(col, row) callable."""
    args = {k: v for k, v in kwargs.items() if k != "numbering"}
    numbering = kwargs.get("numbering")
    if numbering is not None:
        grid = kwargs.get("grid")
        if grid is None:
            problems.append("params.numbering: needs a grid (params.grid)")
        else:
            cols, rows = len(grid.xs), len(grid.ys)
            problems.extend(_numbering_problems(numbering, cols, rows, "params.numbering"))
            args = {**args, "numbering": _numbering_fn(numbering, cols, rows)}
    if problems:
        raise FpSpecJsonError(problems)
    return ft.lga_grid(**args)


Builder = Callable[[Mapping[str, Any], Problems], FootprintSpec]


def _direct(template_fn: Callable[..., FootprintSpec]) -> Builder:
    """A builder that passes the converted params straight to the template."""
    def build(kwargs: Mapping[str, Any], _problems: Problems) -> FootprintSpec:
        return template_fn(**kwargs)
    return build


_BUILDERS: dict[str, Builder] = {
    "lga_grid": _lga_grid, "qfn": _direct(ft.qfn), "dfn": _direct(ft.dfn),
    "lcc_castellated": _direct(ft.lcc_castellated), "sop": _direct(ft.sop),
    "pin_header": _direct(ft.pin_header), "lg290p": _direct(ft.lg290p)}


# ---------------------------------------------------------------------------
# Size limit (an upper bound of the pad entries, computed before building)
# ---------------------------------------------------------------------------
def _grid_cells(grid: ft.PadGrid | None) -> int:
    return 0 if grid is None else len(grid.xs) * len(grid.ys)


_PIN_ENTRIES: dict[str, Callable[[Mapping[str, Any]], int]] = {
    "lga_grid": lambda kw: (sum(len(row.positions) for row in kw.get("edge_rows", ()))
                            + _grid_cells(kw.get("grid"))),
    "qfn": lambda kw: 2 * (kw["pins_x"] + kw["pins_y"]),
    "dfn": lambda kw: 2 * kw["pins_per_side"],
    "lcc_castellated": lambda kw: sum(kw["pins"]),
    "sop": lambda kw: kw["pins"],
    "pin_header": lambda kw: kw["rows"] * kw.get("cols", 1),
    "lg290p": lambda kw: 0,                      # fixed geometry, 79 pads
}


def _ep_entries(ep: ft.ExposedPad | None) -> int:
    """Copper, paste windows, thermal vias and the bottom copy of an exposed pad."""
    if ep is None:
        return 0
    vias = 0 if ep.vias is None else ep.vias.nx * ep.vias.ny + 1
    return 1 + ep.paste_grid[0] * ep.paste_grid[1] + vias


def _check_size(template: str, kwargs: Mapping[str, Any]) -> None:
    """FpSpecJsonError when the footprint could have more than MAX_PADS pad entries."""
    n = _PIN_ENTRIES[template](kwargs) + _ep_entries(kwargs.get("ep"))
    if n > MAX_PADS:
        raise FpSpecJsonError([f"params describe up to {n} pads (copper, paste windows and "
                               f"vias); the limit is {MAX_PADS}"])


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
def _check_template(template: Any) -> str:
    if isinstance(template, str) and template in _SCHEMAS:
        return template
    near = difflib.get_close_matches(str(template), list(TEMPLATES), n=1)
    hint = f" (did you mean {near[0]!r}?)" if near else ""
    raise FpSpecJsonError([f"unknown template {template!r}{hint}; "
                           f"templates: {', '.join(TEMPLATES)}"])


def template_keys(template: str) -> dict[str, list[str]]:
    """{"required": [...], "optional": [...]} params keys of ``template`` (sorted)."""
    fields = {**_SCHEMAS[_check_template(template)], **_COMMON}
    return {"required": sorted(k for k, (_, req) in fields.items() if req),
            "optional": sorted(k for k, (_, req) in fields.items() if not req)}


def _params(template: str, params: Any) -> Any:
    if not isinstance(params, str):
        return params
    try:
        return json.loads(params)
    except ValueError as e:
        raise FpSpecJsonError([f"{template}: params is not valid JSON: {e}"]) from e


def spec_from_json(template: str, params: Mapping[str, Any] | str) -> FootprintSpec:
    """The FootprintSpec ``template`` builds from ``params`` (a JSON object, or
    its text; see the module doc for the keys). Raises FpSpecJsonError naming
    every problem (unknown, missing or mistyped keys, template errors); never
    mutates ``params``."""
    _check_template(template)
    problems: Problems = []
    kwargs = _object(_params(template, params), "params", problems,
                     {**_SCHEMAS[template], **_COMMON})
    try:
        if kwargs is _BAD:
            raise FpSpecJsonError(problems)
        _check_size(template, kwargs)
        return _BUILDERS[template](kwargs, problems)
    except FpSpecJsonError as e:
        raise FpSpecJsonError([f"{template}: {p}" for p in e.problems]) from None
    except (TypeError, ValueError) as e:
        raise FpSpecJsonError([f"{template}: {e}"]) from e
