"""KiCad 11 IPC-API schematic backend.

    from backends.sch_api import SchematicApi, SchematicApiError, build_from_netlist

    with SchematicApi.open("x.kicad_sch", create=True, overwrite=True, mode="headless") as api:
        api.place_symbol("Device:R", "R1", 50.8, 50.8, value="10k")
        api.place_symbol("Device:C", "C1", 76.2, 50.8, rot=90)
        api.wire_pins("R1", "2", "C1", "1")          # straight or L-routed, on grid
        api.power_pin("C1", "2", "GND")              # power:GND symbol on the pin
        api.netlist()                                # {"GND": [["#PWR01","1"], ["C1","2"]], ...}
        api.save(); api.erc()                        # ERC with the newest kicad-cli

``open`` modes: ``headless`` launches a private ``kicad-cli api-server``,
``gui`` joins a running KiCad 11 that has the file open, ``auto`` picks gui
when the file is open there, else headless. Pass ``session=`` to share one
KiCadSession (and its ~6 s library load) across documents; the session's mode
then applies and ``close()`` only closes the document.

Value / Footprint given to ``place_symbol`` are DEFERRED (see its docstring):
reads (symbols / netlist / labels) never push them to KiCad, ``save`` does and
then reloads the saved file so later placements of the same lib_id stay
linked to their library; ``erc`` and ``render_svg`` save first for the same
reason.

Every failure raises SchematicApiError (PinNotFoundError for a pin that does
not resolve) with a message that says what to do; nothing is swallowed.
Coordinates are millimetres on the sheet (+Y down), snapped to the 1.27 mm grid
on the way in. Pin positions for any rotation/mirror use
``sch_geometry.transform_offset`` with the instance's read-back transform,
which was verified against KiCad's own netlist.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

import kicad_env as env
from backends import kipy_bridge as kb
from backends import sch_erc
from backends import sch_geometry as geo
from backends import sch_items as items
from backends import sch_session as sess
from backends.errors import PinNotFoundError, SchematicApiError

__all__ = ["SchematicApi", "SchematicApiError", "PinNotFoundError", "build_from_netlist",
           "BACKEND", "PWR_FLAG_LIB_ID"]

BACKEND = "api"
SCH_SUFFIX = ".kicad_sch"
POWER_LIB = "power"
PWR_FLAG_LIB_ID = "power:PWR_FLAG"
POWER_REF_PREFIX = "#PWR"
FLAG_REF_PREFIX = "#FLG"
Point = tuple[float, float]


def _check_ref(ref: str) -> str:
    if not isinstance(ref, str) or not ref.strip() or any(ch.isspace() for ch in ref):
        raise SchematicApiError(f"reference must be a non-empty string without spaces, got {ref!r}")
    return ref


def _check_lib_id(lib_id: str) -> str:
    nickname, sep, name = str(lib_id).partition(":")
    if not (nickname and sep and name):
        raise SchematicApiError(f"lib_id must look like 'Library:Symbol', got {lib_id!r}")
    return str(lib_id)


def _check_net(net: str) -> str:
    if not isinstance(net, str) or not net.strip():
        raise SchematicApiError(f"net name must be a non-empty string, got {net!r}")
    return net


def _check_unit(unit: int) -> int:
    if not isinstance(unit, int) or isinstance(unit, bool) or unit < 1:
        raise SchematicApiError(f"unit must be a positive integer, got {unit!r}")
    return unit


def _snap(x_mm: float, y_mm: float) -> Point:
    try:
        return geo.snap_point(x_mm, y_mm)
    except (TypeError, ValueError) as exc:
        raise SchematicApiError(f"bad coordinates ({x_mm!r}, {y_mm!r}): {exc}") from exc


class SchematicApi:
    """One open schematic document driven through the KiCad 11 API."""

    def __init__(self, session: sess.KiCadSession, path: str, owns_session: bool) -> None:
        self._session = session
        self._path = path
        self._owns = owns_session
        self._sch = session.schematic_for(path)
        self._cache: list | None = None       # symbol instances; None = re-read from KiCad
        self._pending: dict[tuple[str, int], tuple[str, str]] = {}   # (ref, unit) -> fields
        self._closed = False              # this object: no further operations
        self._document_closed = False     # the headless document was closed already

    # --- availability -------------------------------------------------------
    @staticmethod
    def unavailable_reason() -> str | None:
        """Why the API backend cannot run here (None when it can)."""
        if kb.IMPORT_ERROR:
            return f"vendored kipy not importable ({kb.IMPORT_ERROR}); {kb.INSTALL_HINT}"
        if env.pick_api() is None:
            seen = [f"{i.version} at {i.root}" for i in env.discover()] or ["none"]
            return ("no KiCad with `kicad-cli api-server` found (needs a KiCad 11 nightly, "
                    f"10.99+); installs seen: {seen}")
        return None

    @staticmethod
    def available() -> bool:
        return SchematicApi.unavailable_reason() is None

    # --- open / close -------------------------------------------------------
    @classmethod
    def open(cls, path: str | os.PathLike, create: bool = False, overwrite: bool = False,
             mode: str = "auto", session: sess.KiCadSession | None = None) -> SchematicApi:
        """Open (or with create=True make) the schematic at ``path``."""
        kb.require()
        target = os.path.abspath(str(path))
        if not target.lower().endswith(SCH_SUFFIX):
            raise SchematicApiError(f"a schematic path must end with {SCH_SUFFIX}: {target}")
        exists = Path(target).is_file()
        if not create and not exists:
            raise SchematicApiError(f"{target} not found; pass create=True to make a new schematic")
        if create and exists and not overwrite:
            raise SchematicApiError(f"{target} exists; pass overwrite=True to replace it")
        owns = session is None
        session = session or sess.connect(mode, target)
        try:
            cls._bring_up(session, target, create)
            return cls(session, target, owns)
        except SchematicApiError:
            if owns:
                session.close()
            raise

    @staticmethod
    def _bring_up(session: sess.KiCadSession, target: str, create: bool) -> None:
        if session.mode == "gui":
            if create:
                raise SchematicApiError(
                    f"{target} is open in the KiCad GUI; close it there or use mode='headless' "
                    "to create a new file")
            return                                   # the GUI already holds it
        if create:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            session.create_document(target)
        else:
            session.open_document(target)

    def close(self) -> None:
        """Close the document; an OWNED session is always released as well
        (that stops a headless api-server), even after ``discard``."""
        if self._closed:
            return
        self._closed = True
        if self._session.mode == "headless" and not self._owns:
            self._close_document()
        if self._owns:
            self._session.close()

    def discard(self) -> None:
        """Drop unsaved changes (including deferred field updates), close the
        document (headless; a GUI document is left alone) and release an
        owned session. Equivalent to ``close()`` for a document that was
        never saved, explicit about the intent for one that was."""
        self._pending = {}
        if self._session.mode == "headless" and not self._closed:
            self._close_document()
        self.close()

    def _close_document(self) -> None:
        if self._document_closed:
            return
        self._document_closed = True
        try:
            self._session.close_all_documents()
        except SchematicApiError:
            self._call("reverting", self._sch.revert)
            self._session.close_all_documents()

    def __enter__(self) -> SchematicApi:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # --- properties ---------------------------------------------------------
    @property
    def mode(self) -> str:
        return self._session.mode

    @property
    def path(self) -> str:
        return self._path

    @property
    def kicad_version(self) -> str:
        return self._session.kicad_version

    @property
    def session(self) -> sess.KiCadSession:
        return self._session

    def load_libraries(self) -> None:
        self._session.load_libraries()

    # --- symbols ------------------------------------------------------------
    def place_symbol(self, lib_id: str, ref: str, x_mm: float, y_mm: float, rot: int = 0,
                     unit: int = 1, value: str = "", footprint: str = "") -> dict:
        """Place a library symbol (grid-snapped). Returns {ref, lib_id, unit,
        at: [x, y, rot], pins: [{number, name, x_mm, y_mm, type}], units, ...}.

        ``value`` / ``footprint`` are DEFERRED: KiCad 11 treats an embedded
        library symbol as edited once an instance of it is updated (an
        UpdateItems that changes nothing does it too), and gives a later
        placement of the same lib_id a private renamed copy ("R_1"; the units
        of a multi-unit part then lose their ``Library:`` link). The fields
        are therefore kept here (``symbols`` overlays them; ``netlist`` and
        ``labels`` never need them) and applied by ``flush_fields`` only from
        ``save`` / ``save_as`` / ``set_fields``. ``save`` then reloads the
        saved file (headless), which re-syncs the embedded symbols with the
        library, so placing more of the same lib_id after a save is safe;
        ``erc`` and ``render_svg`` save first for that reason. In GUI mode
        no reload happens: save before placing more of an already-updated
        lib_id and expect KiCad to rename the copy if you do not."""
        self._check_open()
        _check_lib_id(lib_id)
        _check_ref(ref)
        _check_unit(unit)
        try:
            deg = geo.orientation_deg(rot)
        except ValueError as exc:
            raise SchematicApiError(str(exc)) from exc
        x, y = _snap(x_mm, y_mm)
        self.load_libraries()
        try:
            inst = self._sch.place_symbol_from_library(
                lib_id, kb.vec(x, y), orientation=kb.DEG_TO_ORIENT[deg], unit=unit, reference=ref)
        except kb.KIPY_EXCEPTIONS as exc:
            hint = f"; {sess.LIB_TABLE_HINT}" if "not found" in str(exc).lower() else ""
            raise SchematicApiError(f"placing {lib_id} as {ref}: {exc}{hint}") from exc
        self._remember(inst)
        out = items.symbol_dict(inst, with_pins=True)
        if value or footprint:
            self._defer_fields(ref, inst.unit, value, footprint)
            out.update(value=value or out["value"], footprint=footprint or out["footprint"])
        return out

    def _defer_fields(self, ref: str, unit: int, value: str, footprint: str) -> None:
        old_value, old_footprint = self._pending.get((ref, unit), ("", ""))
        self._pending[(ref, unit)] = (value or old_value, footprint or old_footprint)

    @property
    def pending_fields(self) -> int:
        """Deferred Value/Footprint updates not yet written to KiCad."""
        return len(self._pending)

    def flush_fields(self) -> int:
        """Apply every deferred Value/Footprint update; returns how many."""
        pending, self._pending = self._pending, {}
        for (ref, unit), (value, footprint) in pending.items():
            inst = next((i for i in self._instances_of(ref) if i.unit == unit), None)
            if inst is None:
                raise SchematicApiError(f"cannot set fields of {ref} unit {unit}: it is no longer placed")
            self._remember(self._set_fields(inst, value, footprint))
        return len(pending)

    def set_fields(self, ref: str, value: str = "", footprint: str = "",
                   unit: int | None = None) -> dict:
        """Set Value and/or Footprint on a placed symbol NOW: every placed unit
        of ``ref`` (units do not share fields in KiCad) or only ``unit``.
        Applies any deferred updates first."""
        self._check_open()
        _check_ref(ref)
        if not (value or footprint):
            raise SchematicApiError(f"set_fields({ref}): give a value and/or a footprint")
        self.flush_fields()
        placed = self._instances_of(ref)
        targets = [inst for inst in placed if unit is None or inst.unit == unit]
        if not targets:
            raise PinNotFoundError(f"{ref} unit {unit} is not placed "
                                   f"(placed units: {sorted(i.unit for i in placed)})")
        updated = [self._set_fields(inst, value, footprint) for inst in targets]
        for inst in updated:
            self._remember(inst)
        return {"ref": ref, "units": sorted(inst.unit for inst in updated),
                "value": value, "footprint": footprint}

    def pin_xy(self, ref: str, pin: str, unit: int | None = None) -> Point:
        """Absolute (x_mm, y_mm) of a pin (by number or name) for any
        rotation/mirror; ``unit`` picks the placement of a multi-unit part."""
        inst, pin_obj = self._resolve_pin(ref, pin, unit)
        return items.pin_world(inst, pin_obj)

    def pin_info(self, ref: str) -> list[dict]:
        """[{number, name, unit, type}] for every pin of ref's symbol."""
        return items.pin_info(self._instances_of(ref)[0])

    # --- wires --------------------------------------------------------------
    def add_wire(self, x1: float, y1: float, x2: float, y2: float) -> dict:
        """One straight wire between two grid-snapped points."""
        a, b = _snap(x1, y1), _snap(x2, y2)
        if a == b:
            raise SchematicApiError(f"wire from {a} to {b} has zero length after grid snapping")
        self._create([items.wire_item(*a, *b)])
        return {"wires": [[*a, *b]]}

    def wire_pins(self, ref_a: str, pin_a: str, ref_b: str, pin_b: str) -> dict:
        """Wire two pins: straight when aligned, else horizontal-then-vertical."""
        a = self.pin_xy(ref_a, pin_a)
        b = self.pin_xy(ref_b, pin_b)
        segments = geo.route(a, b)
        if not segments:
            raise SchematicApiError(f"{ref_a}.{pin_a} and {ref_b}.{pin_b} sit on the same point {a}")
        self._create([items.wire_item(*seg) for seg in segments])
        return {"from": {"ref": ref_a, "pin": str(pin_a), "x_mm": a[0], "y_mm": a[1]},
                "to": {"ref": ref_b, "pin": str(pin_b), "x_mm": b[0], "y_mm": b[1]},
                "wires": [list(seg) for seg in segments]}

    # --- labels -------------------------------------------------------------
    def add_label(self, net: str, x_mm: float, y_mm: float) -> dict:
        return self.add_labels([(net, x_mm, y_mm)])[0]

    def add_labels(self, specs: Iterable[tuple[str, float, float]]) -> list[dict]:
        """Local labels in one round trip: [(net, x_mm, y_mm), ...]."""
        clean = [(_check_net(net), *_snap(x, y)) for net, x, y in specs]
        if clean:
            self._create([items.label_item(*spec) for spec in clean])
        return [{"net": net, "x_mm": x, "y_mm": y, "kind": "local"} for net, x, y in clean]

    def label_pin(self, ref: str, pin: str, net: str) -> dict:
        x, y = self.pin_xy(ref, pin)
        return {**self.add_label(net, x, y), "ref": ref, "pin": str(pin)}

    def add_global_label(self, net: str, x_mm: float, y_mm: float, shape: str = "passive") -> dict:
        _check_net(net)
        x, y = _snap(x_mm, y_mm)
        self._create([items.global_label_item(net, x, y, kb.label_shape(shape))])
        return {"net": net, "x_mm": x, "y_mm": y, "kind": "global", "shape": shape}

    # --- power --------------------------------------------------------------
    def power_pin(self, ref: str, pin: str, net: str) -> dict:
        """Tie a pin to a power net: a power:<net> symbol when the library has
        one, else a global label."""
        _check_net(net)
        x, y = self.pin_xy(ref, pin)
        lib_id = f"{POWER_LIB}:{net}"
        on = {"ref": ref, "pin": str(pin)}
        if not self._session.has_library_symbol(lib_id):
            label = self.add_global_label(net, x, y)
            return {**label, "on": on, "reason": f"{lib_id} is not in the power library"}
        power_ref = self._next_ref(POWER_REF_PREFIX)
        self._place_pin_at(lib_id, power_ref, x, y)
        return {"kind": "power_symbol", "ref": power_ref, "lib_id": lib_id, "net": net,
                "x_mm": x, "y_mm": y, "on": on}

    def add_power_flag(self, net: str, x_mm: float, y_mm: float) -> dict:
        """A power:PWR_FLAG plus a local label for ``net`` at one point (this
        is how a passively-driven rail is declared a power source for ERC)."""
        _check_net(net)
        x, y = _snap(x_mm, y_mm)
        flag_ref = self._next_ref(FLAG_REF_PREFIX)
        self._place_pin_at(PWR_FLAG_LIB_ID, flag_ref, x, y)
        self.add_label(net, x, y)
        return {"kind": "power_flag", "ref": flag_ref, "net": net, "x_mm": x, "y_mm": y}

    def _place_pin_at(self, lib_id: str, ref: str, x: float, y: float, pin: str = "1") -> dict:
        """Place a one-pin symbol so that its pin lands exactly on (x, y)."""
        placed = self.place_symbol(lib_id, ref, x, y)
        pins = placed["pins"]
        if not pins:
            raise SchematicApiError(f"{lib_id} has no pins to attach at ({x}, {y})")
        target = next((p for p in pins if p["number"] == pin), pins[0])
        dx, dy = target["x_mm"] - x, target["y_mm"] - y
        if abs(dx) > 1e-6 or abs(dy) > 1e-6:
            self._move(ref, x - dx, y - dy)
        return placed

    def _move(self, ref: str, x: float, y: float) -> None:
        inst = self._instances_of(ref)[0]
        inst.position = kb.vec(x, y)
        items.prime_definition(inst)
        updated = self._call(f"moving {ref}", self._sch.update_items, inst)
        if not updated:
            raise SchematicApiError(f"KiCad did not apply the move of {ref}")
        self._remember(updated[0])

    def _next_ref(self, prefix: str) -> str:
        used = {inst.reference for inst in self._instances()}
        number = 1
        while f"{prefix}{number:02d}" in used:
            number += 1
        return f"{prefix}{number:02d}"

    # --- markers ------------------------------------------------------------
    def no_connect(self, ref: str, pin: str) -> dict:
        x, y = self.pin_xy(ref, pin)
        self.add_no_connects([(x, y)])
        return {"ref": ref, "pin": str(pin), "x_mm": x, "y_mm": y}

    def add_no_connects(self, points: Iterable[Point]) -> list[dict]:
        clean = [_snap(x, y) for x, y in points]
        if clean:
            self._create([items.no_connect_item(*pt) for pt in clean])
        return [{"x_mm": x, "y_mm": y} for x, y in clean]

    def add_junction(self, x_mm: float, y_mm: float) -> dict:
        x, y = _snap(x_mm, y_mm)
        self._create([items.junction_item(x, y)])
        return {"x_mm": x, "y_mm": y}

    # --- queries ------------------------------------------------------------
    def symbols(self) -> list[dict]:
        """[{ref, lib_id, value, footprint, unit, at: [x, y, rot], ...}],
        re-read from KiCad with the deferred Value/Footprint overlaid (a read
        never pushes them: see place_symbol)."""
        self._cache = None
        return [self._overlay_pending(items.symbol_dict(inst)) for inst in self._instances()]

    def _overlay_pending(self, symbol: dict) -> dict:
        pending = self._pending.get((symbol["ref"], symbol["unit"]))
        if pending is None:
            return symbol
        value, footprint = pending
        return {**symbol, "value": value or symbol["value"],
                "footprint": footprint or symbol["footprint"]}

    def netlist(self) -> dict[str, list[list[str]]]:
        """{net: [[ref, pin], ...]} from KiCad's connectivity (leading '/' stripped)."""
        self._cache = None
        pins = items.pin_map(self._instances())
        nets = self._call("reading the netlist", self._sch.get_netlist)
        out: dict[str, list[list[str]]] = {}
        for net in nets:
            name = net.name[1:] if net.name.startswith("/") else net.name
            ids = [kiid.value for sheet in net.sheets for kiid in sheet.items]
            out[name] = sorted(list(pins[kiid]) for kiid in ids if kiid in pins)
        return dict(sorted(out.items()))

    def labels(self) -> list[dict]:
        """[{net, x_mm, y_mm, kind}] for every local/global/hierarchical label."""
        return [items.label_dict(label)
                for label in self._call("reading labels", self._sch.get_labels)]

    # --- persistence --------------------------------------------------------
    def is_modified(self) -> bool:
        """Unsaved changes in KiCad's document or deferred fields held here."""
        if self._pending:
            return True
        return bool(self._call("querying modified state", self._sch.is_document_modified))

    def save(self) -> str:
        """Apply the deferred fields, save, then (headless) reload the saved
        file so the embedded library symbols match the library again."""
        self._check_open()
        self.flush_fields()
        self._call("saving", self._sch.save)
        self._resync_after_save()
        return self._path

    def _resync_after_save(self) -> None:
        """RevertDocument = re-read the file just written: KiCad's in-memory
        copies of the library symbols (marked edited by UpdateItems) are
        replaced by the file's, which compare equal to the library, so the
        next placement of the same lib_id shares them instead of getting a
        renamed private copy. Verified with kicad-cli 10.99 ERC. Headless
        only: in a GUI a revert would drop the user's undo history."""
        if self._session.mode != "headless":
            return
        self._call("reloading after save", self._sch.revert)
        self._cache = None

    def save_as(self, path: str | os.PathLike, overwrite: bool = True) -> str:
        """Write a copy (with its project) to ``path``; the open document
        keeps its own path and is NOT reloaded (use save() before placing
        more of a lib_id whose fields this call applied)."""
        self._check_open()
        self.flush_fields()
        target = os.path.abspath(str(path))
        if Path(target).exists() and not overwrite:
            raise SchematicApiError(f"{target} exists; pass overwrite=True to replace it")
        self._call(f"saving a copy to {target}", self._sch.save_as, target, overwrite)
        return target

    def erc(self) -> dict:
        """Save if modified, then ERC the file with the newest kicad-cli:
        {ok, violations, types: {type: count}, report, ...}."""
        if self.is_modified():
            self.save()
        return sch_erc.run_erc(self._path)

    def render_svg(self, out_dir: str | os.PathLike) -> list[str]:
        """Plot every sheet to SVG in ``out_dir`` (saving first when modified,
        so the deferred fields are applied AND re-synced); returns the files."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        if self.is_modified():
            self.save()
        result = self._call("exporting SVG", self._sch.export_svg, str(out),
                            sheet_mode=kb.schematic_jobs_pb2.SJSM_ALL_SHEETS)
        if not result.succeeded:
            raise SchematicApiError(f"SVG export of {self._path} failed: {result.message}")
        written = [p for p in result.output_paths if Path(p).is_file()]
        return written or sorted(str(p) for p in out.glob("*.svg"))

    # --- internals ----------------------------------------------------------
    def _check_open(self) -> None:
        if self._closed:
            raise SchematicApiError(f"{self._path} is closed; open it again")

    def _call(self, what: str, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except kb.KIPY_EXCEPTIONS as exc:
            raise SchematicApiError(f"{what} ({self._path}): {exc}") from exc

    def _create(self, new_items: list) -> list:
        created = self._call("creating items", self._sch.create_items, new_items)
        if len(created) != len(new_items):
            raise SchematicApiError(
                f"KiCad created {len(created)} of {len(new_items)} items in {self._path}")
        return created

    def _set_fields(self, inst, value: str, footprint: str):
        if value:
            inst.value = str(value)
        if footprint:
            inst.footprint_field.text.value = str(footprint)
        items.prime_definition(inst)              # keep the embedded lib symbol intact
        updated = self._call(f"setting fields of {inst.reference}", self._sch.update_items, inst)
        if not updated:
            raise SchematicApiError(f"KiCad did not apply the field update to {inst.reference}")
        return updated[0]

    def _instances(self) -> list:
        if self._cache is None:
            self._cache = list(self._call("reading symbols", self._sch.get_symbols))
        return self._cache

    def _instances_of(self, ref: str) -> list:
        found = [inst for inst in self._instances() if inst.reference == ref]
        if not found:
            refs = sorted({inst.reference for inst in self._instances()})
            raise PinNotFoundError(f"no symbol {ref!r} in {self._path}; placed: {refs}")
        return found

    def _remember(self, inst) -> None:
        ident = inst.id.value
        self._cache = [i for i in self._instances() if i.id.value != ident] + [inst]

    def _resolve_pin(self, ref: str, pin: str, unit: int | None = None) -> tuple:
        """(instance, SchematicPin) for a pin number or name, on the placement
        that owns it (or the requested unit)."""
        insts = self._instances_of(ref)
        key = str(pin)
        found = [(inst, p, u) for inst in insts for p, u in items.pins_with_units(inst)
                 if p.number == key]
        found = found or [(inst, p, u) for inst in insts for p, u in items.pins_with_units(inst)
                          if p.name == key]
        if not found:
            numbers = [p.number for p, _u in items.pins_with_units(insts[0])]
            raise PinNotFoundError(f"{ref} has no pin {key!r}; its pins are {numbers}")
        placed_units = sorted(inst.unit for inst in insts)
        if unit is not None:
            match = [(inst, p) for inst, p, _u in found if inst.unit == unit]
            if not match:
                raise PinNotFoundError(f"{ref} unit {unit} is not placed (placed units: {placed_units})")
            return match[0]
        owners = [(inst, p) for inst, p, u in found if items.owns_pin(inst, u)]
        if not owners:
            needed = sorted({u for _i, _p, u in found})
            raise PinNotFoundError(
                f"pin {key} of {ref} belongs to unit {needed} which is not placed "
                f"(placed units: {placed_units}); place that unit first")
        return owners[0]


def build_from_netlist(design: dict, out_path: str, overwrite: bool = True,
                       session: sess.KiCadSession | None = None) -> dict:
    """Build the sch_build.py topology (grid cells, a label per net pin,
    no-connects, PWR_FLAGs) through the API; see backends.sch_netlist_build."""
    from backends.sch_netlist_build import build_from_netlist as _build
    return _build(design, out_path, overwrite=overwrite, session=session)
