"""File-backed schematic model: the fallback backend when KiCad 11's IPC API is absent.

FileSchematic mirrors the SchematicApi surface (server/backends/sch_api.py)
method-for-method — place_symbol / pin_xy / add_wire / wire_pins / add_label /
label_pin / add_global_label / power_pin / no_connect / add_junction / symbols /
labels / netlist / save / save_as / close / render_svg — so kicad_server
dispatches to either backend without caring which one it holds.

The model is a plain dict written through the SAME emitter format as the
headless pipeline (server/tools/sch_build.py): file version 20250114, every
used library symbol flattened and embedded in lib_symbols, per-instance pin
lists, instances / sheet_instances / embedded_fonts. Pin coordinates for the
*_pin methods come from a pins/ sidecar when one exists, otherwise they are
derived from the library symbol itself. power_pin places a global label (the
file model does not instantiate power symbols).

netlist() is read off the model's own geometry (union-find over wire
endpoints, label anchors, junctions and pin coordinates). KiCad's ERC is the
authority; this is the file model's best reading of what it wrote.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

import toolchain as tc

BACKEND = "file"
SCH_FORMAT_VERSION = 20250114
SCH_GENERATOR = "kicad-eda"
SCH_GENERATOR_VERSION = "10.0"
ROTATIONS = (0, 90, 180, 270)
COORD_TOL = 1e-3
_FONT = "(effects (font (size 1.27 1.27)))"
_SYM_CACHE: dict[str, dict] = {}            # symbols_dir -> sch_build lib cache


class FileModelError(ValueError):
    """A file-model operation could not be carried out; the message says what to do."""


# ----------------------------------------------------------------------------
# Symbol libraries (shared with the pipeline: server/tools/sch_build.py)
# ----------------------------------------------------------------------------
def sch_build():
    """Import server/tools/sch_build.py — the pipeline's symbol flattener / emitter
    helpers (flatten, embed, unit_pins, dumps, pin_offsets). Needs sexpdata."""
    if str(tc.TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(tc.TOOLS_DIR))
    try:
        import sch_build as sb  # noqa: E402
    except ImportError as e:
        raise ImportError(f"cannot import {tc.TOOLS_DIR / 'sch_build.py'} ({e}); "
                          "is sexpdata installed in the server venv?") from e
    return sb


def flat_symbol(lib_id: str, symbols_dir: str) -> list:
    """Flattened (extends-resolved) symbol sexp for a Library:Symbol id.
    Raises FileModelError when the library or the symbol cannot be found."""
    lib, sep, name = lib_id.partition(":")
    if not sep or not lib or not name:
        raise FileModelError(f"lib_id '{lib_id}' must be 'Library:Symbol' (e.g. Device:R)")
    sb = sch_build()
    cache = _SYM_CACHE.setdefault(symbols_dir, {})
    try:
        return sb.flatten(cache, symbols_dir, lib, name)
    except sb.SymbolLibError as e:  # library missing / unparsable / symbol absent
        raise FileModelError(str(e)) from e
    except (FileNotFoundError, KeyError) as e:
        raise FileModelError(f"symbol {lib_id} not found under {symbols_dir}: {e}") from e


def pins_sidecar(lib_id: str, near: str) -> dict | None:
    """A pin map {pin name | number: {x, y}} (mm, schematic space, rotation 0)
    from a `pins/` dir next to the schematic, else the server's central pins/
    dir. A sidecar, when present, overrides the symbol-derived offsets."""
    safe = lib_id.replace(":", "_")
    candidates = [Path(near).parent / "pins" / f"{safe}.pins.json",
                  tc.PINS_DIR / f"{safe}.pins.json"]
    for cand in candidates:
        if cand.is_file():
            try:
                return json.loads(cand.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise FileModelError(f"malformed pin sidecar {cand}: {e}") from e
    return None


def library_pin_map(lib_id: str) -> dict[str, dict] | None:
    """{key: {x, y, unit, number}} derived from the KiCad symbol library, keyed
    by number AND name (the same math build_pin_sidecars.py uses); None when
    no symbol library dir is available. Raises FileModelError when the
    library exists but the symbol does not."""
    syms = tc.symbols_dir()
    if not syms:
        return None
    return sch_build().pin_offsets(flat_symbol(lib_id, syms))


def pin_sources(lib_id: str, near: str) -> tuple[dict[str, dict] | None, dict | None]:
    """(library map, sidecar) for a lib_id — either may be None, never both:
    raises FileModelError with the fix when no pin source exists."""
    side = pins_sidecar(lib_id, near)
    try:
        lib = library_pin_map(lib_id)
    except FileModelError:
        if side is None:
            raise
        lib = None                      # sidecar-only symbol (e.g. a custom part)
    if lib is None and side is None:
        raise FileModelError(
            f"Pin coordinates for {lib_id} are unknown: no sidecar at "
            f"pins/{lib_id.replace(':', '_')}.pins.json and the KiCad symbol "
            f"libraries were not found (install KiCad or set KICAD_PATH), or use "
            f"the explicit-coordinate tools (add_wire/add_label/add_power_port).")
    return lib, side


def pin_map(lib_id: str, near: str) -> dict[str, dict]:
    """Pin offsets for a lib_id keyed by number and name: the library map with
    every key a sidecar knows overridden by the sidecar's offset."""
    lib, side = pin_sources(lib_id, near)
    return {**(lib or {}), **(side or {})}


def distinct_pins(pins: dict[str, dict], unit: int) -> list[dict]:
    """Collapse a pin map keyed by BOTH number and name into one entry per pin
    ({number, name, x, y}) of `unit`. Library-derived maps carry unit/number
    per entry; a sidecar-only map does not, so its keys are grouped by offset
    (pins stacked on one offset — e.g. every unit's output of an op-amp —
    collapse into one entry; the library map is preferred whenever it exists)."""
    if any("number" in v for v in pins.values()):
        out = []
        for key, v in pins.items():
            if v.get("number") != key or v.get("unit", unit) != unit:
                continue
            name = next((k for k, w in pins.items()
                         if w.get("number") == key and k != key), "")
            out.append({"number": key, "name": name, "x": float(v["x"]), "y": float(v["y"])})
        return out
    groups: dict[tuple[float, float], list[str]] = {}
    for key, v in pins.items():
        groups.setdefault((float(v["x"]), float(v["y"])), []).append(key)
    out = []
    for (x, y), keys in groups.items():
        number = next((k for k in keys if k.isdigit()), min(keys, key=len))
        name = next((k for k in keys if not k.isdigit() and k != number), "")
        out.append({"number": number, "name": name, "x": x, "y": y})
    return out


def rotate_offset(dx: float, dy: float, rot: int) -> tuple[float, float]:
    """Rotate a rotation-0 schematic-space pin offset by the symbol's (at ... rot).
    KiCad rotates counter-clockwise on screen; the sheet is +Y down."""
    r = int(rot) % 360
    if r == 0:
        return dx, dy
    if r == 90:
        return dy, -dx
    if r == 180:
        return -dx, -dy
    if r == 270:
        return -dy, dx
    raise FileModelError(f"rotation must be one of 0/90/180/270, got {rot}")


def validate_component(c: dict[str, Any]) -> dict[str, Any]:
    """Validate one component dict ({ref, lib_id, x, y, [value], [rot],
    [footprint], [unit]}) and build the model entry. Raises FileModelError."""
    for key in ("ref", "lib_id", "x", "y"):
        if key not in c:
            raise FileModelError(f"component is missing '{key}': {c}")
    lib_id = str(c["lib_id"])
    if lib_id.count(":") != 1 or not all(lib_id.split(":")):
        raise FileModelError(f"lib_id '{lib_id}' must be 'Library:Symbol' (e.g. Device:R)")
    rot = int(c.get("rot", 0))
    if rot not in ROTATIONS:
        raise FileModelError(f"rot must be one of 0/90/180/270 for {c['ref']}, got {rot}")
    return {"ref": str(c["ref"]), "lib_id": lib_id, "value": str(c.get("value", "")),
            "x": float(c["x"]), "y": float(c["y"]), "rot": rot,
            "unit": int(c.get("unit", 1)), "footprint": str(c.get("footprint", "")),
            "uuid": str(uuid.uuid4())}


def new_model(path: str, paper: str = "A4") -> dict[str, Any]:
    return {"uuid": str(uuid.uuid4()), "paper": paper, "project": Path(path).stem,
            "symbols": [], "wires": [], "labels": [], "power": [],
            "no_connects": [], "junctions": []}


# ----------------------------------------------------------------------------
# S-expression emission — the same file format as server/tools/sch_build.py
# ----------------------------------------------------------------------------
def _q(s: Any) -> str:
    """Quote a string atom for a KiCad S-expression."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _lib_symbols(model: dict[str, Any], symbols_dir: str | None
                 ) -> tuple[list[str], dict[str, dict[int, list[str]]], list[str]]:
    """Flatten + embed every distinct lib_id in the model.
    Returns (lib_symbols lines, {lib_id: {unit: [pin numbers]}}, unresolved lib_ids)."""
    lib_ids = list(dict.fromkeys(s["lib_id"] for s in model["symbols"]))
    if not symbols_dir or not lib_ids:
        return [], {}, []
    sb = sch_build()
    blocks: list[str] = []
    units: dict[str, dict[int, list[str]]] = {}
    unresolved: list[str] = []
    for lib_id in lib_ids:
        try:
            flat = flat_symbol(lib_id, symbols_dir)
        except FileModelError:
            unresolved.append(lib_id)
            continue
        blocks.append("\t\t" + sb.dumps(sb.embed(flat, lib_id)))
        units[lib_id] = sb.unit_pins(flat)
    return blocks, units, unresolved


def _symbol_lines(s: dict[str, Any], unit_pins: dict[int, list[str]],
                  root: str, project: str) -> list[str]:
    x, y, rot, unit = float(s["x"]), float(s["y"]), int(s.get("rot", 0)), int(s.get("unit", 1))
    lines = [
        "\t(symbol",
        f"\t\t(lib_id {_q(s['lib_id'])})",
        f"\t\t(at {x:.4f} {y:.4f} {rot})",
        f"\t\t(unit {unit})",
        "\t\t(exclude_from_sim no)", "\t\t(in_bom yes)", "\t\t(on_board yes)", "\t\t(dnp no)",
        f"\t\t(uuid \"{s['uuid']}\")",
        f"\t\t(property \"Reference\" {_q(s['ref'])} (at {x:.4f} {y - 7.62:.4f} 0) {_FONT})",
        f"\t\t(property \"Value\" {_q(s.get('value', ''))} (at {x:.4f} {y + 7.62:.4f} 0) {_FONT})",
        f"\t\t(property \"Footprint\" {_q(s.get('footprint', ''))} (at {x:.4f} {y:.4f} 0) "
        "(effects (font (size 1.27 1.27)) (hide yes)))",
    ]
    lines += [f"\t\t(pin {_q(num)} (uuid \"{uuid.uuid4()}\"))"
              for num in unit_pins.get(unit, [])]
    lines += [f"\t\t(instances (project {_q(project)} (path \"/{root}\" "
              f"(reference {_q(s['ref'])}) (unit {unit}))))", "\t)"]
    return lines


def _item_lines(model: dict[str, Any]) -> list[str]:
    """Wires, labels, global labels, no-connects and junctions."""
    L: list[str] = []
    for w in model["wires"]:
        (x1, y1), (x2, y2) = w["a"], w["b"]
        L.append(f"\t(wire (pts (xy {x1:.4f} {y1:.4f}) (xy {x2:.4f} {y2:.4f})) "
                 f"(stroke (width 0) (type default)) (uuid \"{uuid.uuid4()}\"))")
    for lb in model["labels"]:
        L.append(f"\t(label {_q(lb['net'])} (at {lb['x']:.4f} {lb['y']:.4f} {int(lb.get('rot', 0))}) "
                 f"(effects (font (size 1.27 1.27)) (justify left bottom)) (uuid \"{uuid.uuid4()}\"))")
    for p in model["power"]:
        L.append(f"\t(global_label {_q(p['net'])} (shape input) (at {p['x']:.4f} {p['y']:.4f} 0) "
                 f"{_FONT} (uuid \"{uuid.uuid4()}\"))")
    for nc in model["no_connects"]:
        L.append(f"\t(no_connect (at {nc['x']:.4f} {nc['y']:.4f}) (uuid \"{uuid.uuid4()}\"))")
    for j in model["junctions"]:
        L.append(f"\t(junction (at {j['x']:.4f} {j['y']:.4f}) (diameter 0) (color 0 0 0 0) "
                 f"(uuid \"{uuid.uuid4()}\"))")
    return L


def emit(model: dict[str, Any], symbols_dir: str | None) -> tuple[str, list[str]]:
    """Render the model as a KiCad 9/10-format .kicad_sch (identical structure to
    sch_build.py's output). Returns (text, lib_ids that could not be embedded)."""
    blocks, units, unresolved = _lib_symbols(model, symbols_dir)
    root, project = model["uuid"], model.get("project", "design")
    L = ["(kicad_sch", f"\t(version {SCH_FORMAT_VERSION})",
         f"\t(generator {_q(SCH_GENERATOR)})",
         f"\t(generator_version {_q(SCH_GENERATOR_VERSION)})",
         f"\t(uuid \"{root}\")", f"\t(paper {_q(model['paper'])})",
         "\t(lib_symbols", *blocks, "\t)"]
    for s in model["symbols"]:
        L += _symbol_lines(s, units.get(s["lib_id"], {}), root, project)
    L += _item_lines(model)
    L += ["\t(sheet_instances", "\t\t(path \"/\" (page \"1\"))", "\t)",
          "\t(embedded_fonts no)", ")"]
    return "\n".join(L) + "\n", unresolved


def save_result(m: dict[str, Any], sch: str, symbols_dir: str | None,
                unresolved: list[str]) -> dict:
    res = {"ok": True, "path": sch, "symbols": len(m["symbols"]),
           "wires": len(m["wires"]), "labels": len(m["labels"]),
           "power_ports": len(m["power"]), "no_connects": len(m["no_connects"]),
           "junctions": len(m["junctions"])}
    if not symbols_dir:
        return {**res, "warning": "lib_symbols empty: KiCad symbol libraries not found; "
                                  "KiCad will rebuild from its configured libraries on open"}
    if unresolved:
        return {**res, "ok": False, "written": True,
                "unresolved_symbols": unresolved,
                "reason": f"file written but lib_symbols is incomplete: {unresolved} not "
                          f"found under {symbols_dir}. Fix the lib_id(s) (Library:Symbol, "
                          f"e.g. Device:R) and call save_schematic again."}
    return res


# ----------------------------------------------------------------------------
# Connectivity (the file model's own reading of its geometry)
# ----------------------------------------------------------------------------
Point = tuple[float, float]


def _pt(x: float, y: float) -> Point:
    return (round(float(x), 3), round(float(y), 3))


def _on_segment(p: Point, a: Point, b: Point) -> bool:
    """True when p lies on the segment a-b (collinear and inside its bounds)."""
    (px, py), (ax, ay), (bx, by) = p, a, b
    if abs((bx - ax) * (py - ay) - (by - ay) * (px - ax)) > COORD_TOL:
        return False
    return (min(ax, bx) - COORD_TOL <= px <= max(ax, bx) + COORD_TOL
            and min(ay, by) - COORD_TOL <= py <= max(ay, by) + COORD_TOL)


class _Union:
    """Union-find over points."""

    def __init__(self) -> None:
        self.parent: dict[Point, Point] = {}

    def find(self, k: Point) -> Point:
        self.parent.setdefault(k, k)
        while self.parent[k] != k:
            self.parent[k] = self.parent[self.parent[k]]
            k = self.parent[k]
        return k

    def union(self, a: Point, b: Point) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _join_geometry(model: dict[str, Any]) -> _Union:
    """Wires join their endpoints; an endpoint or a junction lying on another
    wire joins that wire too (KiCad's implicit / explicit junction rule)."""
    uf = _Union()
    wires = [(_pt(*w["a"]), _pt(*w["b"])) for w in model["wires"]]
    for a, b in wires:
        uf.union(a, b)
    touch = [p for a, b in wires for p in (a, b)]
    touch += [_pt(j["x"], j["y"]) for j in model["junctions"]]
    for p in touch:
        for c, d in wires:
            if p not in (c, d) and _on_segment(p, c, d):
                uf.union(p, c)
    return uf


def connectivity(model: dict[str, Any],
                 pins_of: Callable[[dict], list[dict]]) -> dict[str, list[list[str]]]:
    """{net: [[ref, pin], ...]} read off the model: labels / global labels name
    the point they sit on, pins (from pins_of(symbol) -> [{number, x_mm,
    y_mm}]) join the point they sit on. An unlabeled group is named the way
    KiCad does it, Net-(REF-PadN) after its first pin; a labeled group with no
    pin is reported with an empty list (a dangling label)."""
    uf = _join_geometry(model)
    names: dict[Point, set[str]] = {}
    pins: dict[Point, list[list[str]]] = {}
    for lb in model["labels"] + model["power"]:
        names.setdefault(uf.find(_pt(lb["x"], lb["y"])), set()).add(lb["net"])
    for sym in model["symbols"]:
        for p in pins_of(sym):
            pins.setdefault(uf.find(_pt(p["x_mm"], p["y_mm"])), []).append(
                [sym["ref"], p["number"]])
    nets: dict[str, list[list[str]]] = {}
    for root in set(names) | set(pins):
        members = sorted(pins.get(root, []))
        if root in names:
            name = sorted(names[root])[0]
        else:
            name = f"Net-({members[0][0]}-Pad{members[0][1]})"
        nets[name] = nets.get(name, []) + members
    return nets


# ----------------------------------------------------------------------------
# The backend object
# ----------------------------------------------------------------------------
class FileSchematic:
    """An in-memory schematic for one .kicad_sch path (nothing touches disk until
    save). Method names and shapes follow SchematicApi so kicad_server can
    dispatch to either backend."""

    backend = BACKEND
    mode = "file"

    def __init__(self, path: str, paper: str = "A4") -> None:
        self.path = path
        self.model = new_model(path, paper)
        self.netlist_warnings: list[str] = []

    # --- context manager ------------------------------------------------------
    def __enter__(self) -> "FileSchematic":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        """Nothing to release; the model simply stops being edited."""

    # --- symbols -------------------------------------------------------------
    def place_symbol(self, lib_id: str, ref: str, x_mm: float, y_mm: float,
                     rot: int = 0, unit: int = 1, value: str = "",
                     footprint: str = "") -> dict:
        """Place one symbol. Returns {ref, lib_id, unit, at: [x, y, rot],
        pins: [{number, name, x_mm, y_mm}], uuid}; pins is empty (with a
        pins_note) when neither a sidecar nor the symbol library resolves it."""
        sym = validate_component({"ref": ref, "lib_id": lib_id, "value": value, "x": x_mm,
                                  "y": y_mm, "rot": rot, "footprint": footprint, "unit": unit})
        self.model["symbols"].append(sym)
        res = {"ref": sym["ref"], "lib_id": sym["lib_id"], "unit": sym["unit"],
               "at": [sym["x"], sym["y"], sym["rot"]], "uuid": sym["uuid"]}
        try:
            return {**res, "pins": self._symbol_pins(sym)}
        except FileModelError as e:
            return {**res, "pins": [], "pins_note": str(e)}

    def _symbol_pins(self, sym: dict[str, Any]) -> list[dict]:
        """Every pin of a placed symbol (its unit) at its absolute sheet position:
        the library enumerates the pins, a sidecar overrides their offsets."""
        lib, side = pin_sources(sym["lib_id"], self.path)
        pins = distinct_pins(lib if lib is not None else side, sym["unit"])
        out = []
        for p in pins:
            off = (side or {}).get(p["number"], p)
            dx, dy = rotate_offset(float(off["x"]), float(off["y"]), sym["rot"])
            out.append({"number": p["number"], "name": p["name"],
                        "x_mm": round(sym["x"] + dx, 4), "y_mm": round(sym["y"] + dy, 4)})
        return out

    def _symbol(self, ref: str, pin_unit: int | None, unit: int | None) -> dict[str, Any]:
        """The placed symbol entry for `ref`: the requested unit, else the unit
        the pin belongs to, else the first placement of that reference."""
        placed = [s for s in self.model["symbols"] if s["ref"] == ref]
        if not placed:
            raise FileModelError(f"No component {ref} in {self.path}.")
        for want in (unit, pin_unit):
            if want is not None:
                match = next((s for s in placed if s["unit"] == want), None)
                if match is not None:
                    return match
        if unit is not None:
            raise FileModelError(f"{ref} has no unit {unit} placed in {self.path}.")
        return placed[0]

    def pin_xy(self, ref: str, pin: str, unit: int | None = None) -> tuple[float, float]:
        """Absolute schematic coordinate (mm) of a component pin, by pin number
        or name, rotated by the symbol's placement rotation."""
        lib_id = self._symbol(ref, None, None)["lib_id"]
        lib, side = pin_sources(lib_id, self.path)
        pins = {**(lib or {}), **(side or {})}
        if pin not in pins:
            raise FileModelError(
                f"{lib_id} has no pin '{pin}' (known: {sorted(pins)[:40]}). "
                f"Use the pin number or name as printed in the KiCad symbol.")
        off = pins[pin]
        sym = self._symbol(ref, (lib or {}).get(pin, {}).get("unit"), unit)
        dx, dy = rotate_offset(float(off["x"]), float(off["y"]), sym["rot"])
        return round(sym["x"] + dx, 4), round(sym["y"] + dy, 4)

    # --- wires / labels / flags ------------------------------------------------
    def add_wire(self, x1: float, y1: float, x2: float, y2: float) -> dict:
        wire = {"a": (float(x1), float(y1)), "b": (float(x2), float(y2))}
        self.model["wires"].append(wire)
        return {"wires": [[*wire["a"], *wire["b"]]]}

    def wire_pins(self, ref_a: str, pin_a: str, ref_b: str, pin_b: str) -> dict:
        """Wire two pins: straight when aligned, else an L (horizontal, then
        vertical) through the corner (x_b, y_a)."""
        ax, ay = self.pin_xy(ref_a, pin_a)
        bx, by = self.pin_xy(ref_b, pin_b)
        legs = [(ax, ay, bx, by)] if ax == bx or ay == by else \
            [(ax, ay, bx, ay), (bx, ay, bx, by)]
        wires = [self.add_wire(*leg)["wires"][0] for leg in legs]
        return {"from": f"{ref_a}.{pin_a}", "to": f"{ref_b}.{pin_b}", "wires": wires}

    def add_label(self, net: str, x: float, y: float, rot: int = 0) -> dict:
        self.model["labels"].append({"net": net, "x": float(x), "y": float(y), "rot": int(rot)})
        return {"net": net, "at": [float(x), float(y)]}

    def label_pin(self, ref: str, pin: str, net: str) -> dict:
        x, y = self.pin_xy(ref, pin)
        return {**self.add_label(net, x, y), "pin": f"{ref}.{pin}"}

    def add_global_label(self, net: str, x: float, y: float) -> dict:
        self.model["power"].append({"net": net, "x": float(x), "y": float(y)})
        return {"net": net, "at": [float(x), float(y)], "kind": "global_label"}

    def power_pin(self, ref: str, pin: str, net: str) -> dict:
        """A power port at a pin: the file model places a global label."""
        x, y = self.pin_xy(ref, pin)
        return {**self.add_global_label(net, x, y), "pin": f"{ref}.{pin}"}

    def no_connect(self, ref: str, pin: str) -> dict:
        x, y = self.pin_xy(ref, pin)
        self.model["no_connects"].append({"x": x, "y": y, "pin": [ref, pin]})
        return {"pin": f"{ref}.{pin}", "at": [x, y]}

    def add_junction(self, x: float, y: float) -> dict:
        self.model["junctions"].append({"x": float(x), "y": float(y)})
        return {"at": [float(x), float(y)]}

    # --- readers -----------------------------------------------------------------
    def symbols(self) -> list[dict]:
        return [{"ref": s["ref"], "lib_id": s["lib_id"], "value": s["value"],
                 "unit": s["unit"], "at": [s["x"], s["y"], s["rot"]]}
                for s in self.model["symbols"]]

    def labels(self) -> list[dict]:
        local = [{"net": lb["net"], "x": lb["x"], "y": lb["y"], "kind": "local"}
                 for lb in self.model["labels"]]
        power = [{"net": p["net"], "x": p["x"], "y": p["y"], "kind": "global"}
                 for p in self.model["power"]]
        return local + power

    def netlist(self) -> dict[str, list[list[str]]]:
        """{net: [[ref, pin], ...]} from the model's geometry. Symbols whose pins
        cannot be resolved contribute nothing and are listed in
        netlist_warnings (never silently)."""
        warnings: list[str] = []

        def pins_of(sym: dict) -> list[dict]:
            try:
                return self._symbol_pins(sym)
            except FileModelError as e:
                warnings.append(f"{sym['ref']} ({sym['lib_id']}): {e}")
                return []

        nets = connectivity(self.model, pins_of)
        self.netlist_warnings = warnings
        return nets

    # --- persistence ---------------------------------------------------------------
    def save(self, symbols_dir: str = "") -> dict:
        """Write the .kicad_sch (library symbols embedded from KiCad's stock
        libraries or `symbols_dir`). The caller runs the open-document guard."""
        syms = tc.symbols_dir(symbols_dir)
        text, unresolved = emit(self.model, syms)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.path).write_text(text, encoding="utf-8")
        return save_result(self.model, self.path, syms, unresolved)

    def save_as(self, path: str, symbols_dir: str = "") -> dict:
        if not str(path).endswith(".kicad_sch"):
            raise FileModelError(f"path must end in .kicad_sch, got '{path}'")
        self.path = str(path)
        self.model = {**self.model, "project": Path(path).stem}
        return self.save(symbols_dir)

    def render_svg(self, out_dir: str) -> list[str]:
        """Export the SAVED file to SVG with the newest kicad-cli."""
        cli = tc.kicad_cli()
        if not cli:
            raise FileModelError("kicad-cli not found; install KiCad or set KICAD_PATH.")
        if not Path(self.path).is_file():
            raise FileModelError(f"{self.path} is not on disk yet; call save_schematic first.")
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        r = tc.run([cli, "sch", "export", "svg", "-o", out_dir, self.path],
                   timeout=tc.KICAD_CLI_TIMEOUT_S)
        if r.returncode != 0:
            raise FileModelError(f"kicad-cli sch export svg failed: {r.stderr[-800:]}")
        return sorted(str(p) for p in Path(out_dir).glob("*.svg"))
