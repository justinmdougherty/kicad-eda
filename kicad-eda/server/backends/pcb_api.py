"""KiCad IPC-API board backend: read a board, lay copper, verify, save.

    from backends.pcb_api import BoardApi, BoardApiError

    with BoardApi.open("x.kicad_pcb", mode="headless") as api:
        api.overview()          # layers, outline, rules, classes, planes, footprints, counts
        api.pads(net="GND")     # [{id, ref, pad, net, x, y, layers, through_hole, ...}]
        api.clusters("OUT_A")   # [[pad_id, ...], ...]: one list per connected group
        api.add_tracks("OUT_A", [{"layer": "F.Cu", "start": [10, 10], "end": [14, 10],
                                  "width": 0.25}])
        api.add_vias("OUT_A", [{"x": 14, "y": 10, "diameter": 0.6, "drill": 0.3}])
        api.place_footprint("P_parts:Quectel_LG290P", "U9", 20, 15)   # project fp-lib-table
        api.refill_zones(); api.save()

Modes: ``headless`` launches a private KiCad 11 ``kicad-cli api-server`` with
the board preloaded; ``gui`` joins a running KiCad GUI whose PCB editor has
the file open; ``auto`` picks gui when the file is open there, else
headless. One BoardApi owns one session (a headless server holds one board).
A GUI older than KiCad 11 answers fewer queries and the reads degrade by
version (see pcb_session's GUI VERSIONS note; every degradation is listed in
``notes``): copper layers from the file's layer table below 9.0.5, pad
references from the footprint definitions below 10.0.6, no connectivity
below 10.0.1, no design rules below 11.

FILE FORMAT: ``save()`` / ``save_as()`` through a headless session write the
KiCad 11 board format (``(version 20260901)``, ``(generator_version "10.99")``).
KiCad 9/10 and their kicad-cli / pcbnew Python cannot read the file
afterwards: DRC and fab exports must then use kicad-cli 11
(``toolchain.board_cli(pcb)`` picks the writer's kicad-cli) and the
Freerouting / SWIG tools refuse the board. A GUI session keeps the running
GUI's format. Nothing is written before the first save; ``discard()`` reverts
unsaved changes (headless) and closes.

Units: millimetres in and out (4 decimals); nanometre ints only inside this
module and pcb_items. Every kipy call goes through ``_call`` which turns a
kipy failure into BoardApiError("<what> failed: ..."); an unknown net, copper
layer or item id raises BoardApiError naming the valid ones.
"""
from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable

import kicad_env as env
from backends import kipy_bridge as kb
from backends import pcb_items as items
from backends import pcb_session as sess
from backends.errors import BoardApiError
from backends.sch_session import NO_HANDLER

__all__ = ["BoardApi", "BoardApiError", "BACKEND", "project_netclasses", "file_copper_layers"]

BACKEND = "api"
PCB_SUFFIX = ".kicad_pcb"
PROJECT_SUFFIX = ".kicad_pro"
CONNECTIVITY_MIN_VERSION = (10, 0, 1)      # GetConnectedItems
ENABLED_LAYERS_MIN_VERSION = "9.0.5"       # GetBoardEnabledLayers
PAD_PARENT_MIN_VERSION = "10.0.6"          # Pad.parent in GetItems
HEADER_BYTES = 65536                       # the (layers ...) table sits near the top of the file
_LAYER_TABLE_RE = re.compile(r'\(layers((?:\s*\(\s*\d+\s+"[^"]*"[^()]*\))+)\s*\)')
_COPPER_ENTRY_RE = re.compile(r'\(\s*\d+\s+"((?:F|B|In\d+)\.Cu)"')
DEFAULT_CLASS = "Default"
SEGMENT_KEYS = ("layer", "start", "end", "width")
VIA_KEYS = ("x", "y", "diameter", "drill")
SIDE_LAYERS = {"F": "F.Cu", "B": "B.Cu"}   # place_footprint side -> the layer KiCad takes
# netclass field -> rules() key that backs it when neither KiCad nor the project sets it
CLASS_TO_RULE = {"clearance": "min_clearance", "track_width": "min_track_width",
                 "via_diameter": "min_via_diameter", "via_drill": "min_via_drill"}
Point = tuple[float, float]


# --- project file -------------------------------------------------------------
def _num(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def project_netclasses(pcb_path: str | os.PathLike) -> dict[str, dict]:
    """{class name: {clearance, track_width, via_diameter, via_drill}} (mm)
    from the sibling .kicad_pro's ``net_settings.classes``; {} when the file
    is absent or unparsable (the board minimums then back the classes)."""
    pro = Path(pcb_path).with_suffix(PROJECT_SUFFIX)
    try:
        data = json.loads(pro.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    settings = data.get("net_settings", {}) if isinstance(data, dict) else {}
    classes = settings.get("classes", []) if isinstance(settings, dict) else []
    out: dict[str, dict] = {}
    for cls in classes:
        if isinstance(cls, dict) and cls.get("name"):
            out[str(cls["name"])] = {field: _num(cls.get(field)) for field in items.CLASS_FIELDS}
    return out


def _stackup_key(name: str) -> tuple[int, int]:
    """F.Cu first, In1.Cu .. InN.Cu by number, B.Cu last."""
    if name == "F.Cu":
        return (0, 0)
    if name == "B.Cu":
        return (2, 0)
    return (1, int(name[2:-3]))


def file_copper_layers(pcb_path: str | os.PathLike) -> list[str]:
    """Copper layer names top to bottom from the .kicad_pcb's ``(layers ...)``
    table - the fallback for a KiCad GUI older than 9.0.5, which has no
    GetBoardEnabledLayers; [] when the file is unreadable or has no table."""
    try:
        with open(pcb_path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(HEADER_BYTES)
    except OSError:
        return []
    table = _LAYER_TABLE_RE.search(head)
    if table is None:
        return []
    names = dict.fromkeys(_COPPER_ENTRY_RE.findall(table.group(1)))
    return sorted(names, key=_stackup_key)


def _unsupported(exc: BaseException | None) -> bool:
    """True when KiCad answered that it has NO HANDLER for the request (the
    call does not exist in that KiCad version), as opposed to a failure of a
    call it supports (timeout, busy editor, bad argument)."""
    if exc is None or kb.IMPORT_ERROR:
        return False
    if isinstance(exc, kb.ApiError):
        codes = kb.ApiStatusCode
        return exc.code in (codes.AS_UNHANDLED, codes.AS_UNIMPLEMENTED) or NO_HANDLER in str(exc)
    return False


def _definition_pads(footprint) -> list:
    """The pads inside a FootprintInstance's definition (KiCad serialises them
    with the footprint); [] when this kipy / KiCad reports none."""
    definition = getattr(footprint, "definition", None)
    return list(getattr(definition, "pads", None) or [])


def _fill_class(netclass: dict, project: dict[str, dict], rules: dict) -> dict:
    """A copy of ``netclass`` with every None field taken from the project's
    class of the same name, then from the board minimums."""
    fallback = project.get(netclass["name"], {})
    filled = dict(netclass)
    for field in items.CLASS_FIELDS:
        if filled.get(field) is None:
            filled[field] = fallback.get(field)
        if filled.get(field) is None:
            filled[field] = rules.get(CLASS_TO_RULE[field])
    return filled


def _is_plane(zone: dict, copper: list[str]) -> bool:
    """A copper pour with a net on an inner copper layer (the planes the
    netlist pipeline pours for GND and the first power rail)."""
    inner = [layer for layer in copper if layer not in ("F.Cu", "B.Cu")]
    return bool(zone["net"]) and not zone["rule_area"] and \
        any(layer in inner for layer in zone["layers"])


# --- argument checks ----------------------------------------------------------
def _positive(value, what: str) -> float:
    number = _num(value)
    if number is None:
        raise BoardApiError(f"{what} must be a number in mm, got {value!r}")
    if number <= 0:
        raise BoardApiError(f"{what} must be > 0 mm, got {value!r}")
    return number


def _coord(value, what: str) -> float:
    number = _num(value)
    if number is None:
        raise BoardApiError(f"{what} must be a number in mm, got {value!r}")
    return number


def _point(value, what: str) -> Point:
    try:
        x, y = value
    except (TypeError, ValueError) as exc:
        raise BoardApiError(f"{what} must be [x, y] in mm, got {value!r}") from exc
    return _coord(x, f"{what} x"), _coord(y, f"{what} y")


def _check_segment(segment: dict, copper: list[str]) -> tuple[int, Point, Point, float]:
    """(BoardLayer value, start, end, width) of one add_tracks segment."""
    if not isinstance(segment, dict):
        raise BoardApiError(f"a segment must be a dict with {SEGMENT_KEYS}, got {segment!r}")
    missing = [key for key in SEGMENT_KEYS if key not in segment]
    if missing:
        raise BoardApiError(f"segment {segment!r} lacks {missing}; each needs {SEGMENT_KEYS}")
    layer = str(segment["layer"])
    if layer not in copper:
        raise BoardApiError(
            f"unknown copper layer {layer!r}; this board's copper layers are {copper}")
    start, end = _point(segment["start"], "start"), _point(segment["end"], "end")
    width = _positive(segment["width"], "track width")
    if start == end:
        raise BoardApiError(f"segment on {layer} from {list(start)} to {list(end)} has zero length")
    return items.layer_value(layer), start, end, width


def _check_via(spec: dict) -> tuple[float, float, float, float]:
    """(x, y, diameter, drill) of one add_vias spec."""
    if not isinstance(spec, dict):
        raise BoardApiError(f"a via must be a dict with {VIA_KEYS}, got {spec!r}")
    missing = [key for key in VIA_KEYS if key not in spec]
    if missing:
        raise BoardApiError(f"via {spec!r} lacks {missing}; each needs {VIA_KEYS}")
    x, y = _coord(spec["x"], "via x"), _coord(spec["y"], "via y")
    diameter = _positive(spec["diameter"], "via diameter")
    drill = _positive(spec["drill"], "via drill")
    if drill >= diameter:
        raise BoardApiError(f"via drill {drill} mm must be smaller than its diameter {diameter} mm")
    return x, y, diameter, drill


def _check_lib_id(lib_id: str, table: Path) -> str:
    """``lib_id`` when it is '<nickname>:<footprint>'; the error names the
    project table the nickname must be a row of."""
    nickname, sep, name = lib_id.partition(":") if isinstance(lib_id, str) else ("", "", "")
    if not (nickname.strip() and sep and name.strip()):
        raise BoardApiError(f"lib_id must be '<nickname>:<footprint>' with the nickname a row "
                            f"of {table} or a stock footprint library, got {lib_id!r}")
    return lib_id


def _check_ref(ref: str) -> str:
    if not isinstance(ref, str) or not ref.strip() or any(ch.isspace() for ch in ref):
        raise BoardApiError(f"reference must be a non-empty string without spaces, got {ref!r}")
    return ref


def _side_layer(side: str) -> int:
    """BoardLayer value for side 'F' / 'B' (KiCad places on F.Cu or B.Cu only)."""
    key = side.upper() if isinstance(side, str) else ""
    if key not in SIDE_LAYERS:
        raise BoardApiError(f"side must be 'F' or 'B', got {side!r}")
    return items.layer_value(SIDE_LAYERS[key])


def _degrees(value: Any) -> float:
    number = _num(value)
    if number is None:
        raise BoardApiError(f"rot must be a number in degrees, got {value!r}")
    return number


def _angle(degrees: float) -> Any:
    """A kipy Angle; kipy_bridge exports none yet, and every caller already
    passed kb.require() (BoardApi.open), so kipy imports."""
    from kipy.geometry import Angle
    return Angle.from_degrees(degrees)


# --- union-find over item ids -------------------------------------------------
def _find(parent: dict[str, str], key: str) -> str:
    root = key
    while parent.get(root, root) != root:
        root = parent[root]
    return root


def _union(parent: dict[str, str], a: str, b: str) -> None:
    root_a, root_b = _find(parent, a), _find(parent, b)
    if root_a != root_b:
        parent[root_a] = root_b


class BoardApi:
    """One open board driven through the KiCad IPC API."""

    def __init__(self, session: sess.BoardSession, path: str) -> None:
        self._session = session
        self._path = path
        self._board = session.board(path)
        self._dirty = False
        self._closed = False
        self._connectivity: bool | None = None
        self._notes: list[str] = []

    # --- availability -------------------------------------------------------
    @staticmethod
    def unavailable_reason() -> str | None:
        """Why no HEADLESS board session can run here (None when it can). A
        GUI session may still work through a running KiCad 9/10 GUI (with
        the version degradations the module doc lists)."""
        if kb.IMPORT_ERROR:
            return f"vendored kipy not importable ({kb.IMPORT_ERROR}); {kb.INSTALL_HINT}"
        if env.pick_api() is None:
            return sess.no_api_reason()
        return None

    @staticmethod
    def available() -> bool:
        return BoardApi.unavailable_reason() is None

    # --- open / close -------------------------------------------------------
    @staticmethod
    def resolve_mode(path: str | os.PathLike, mode: str = "auto") -> str:
        """The mode ``open(path, mode)`` would use now: 'gui' when a running
        KiCad GUI has the board open (or mode asks for it), else 'headless'.
        A caller keeps its headless housekeeping (one board per api-server)
        for headless opens only. An unknown mode raises BoardApiError."""
        if mode not in sess.MODES:
            raise BoardApiError(f"mode must be one of {sess.MODES}, got {mode!r}")
        if mode != "auto":
            return mode
        kb.require(BoardApiError)
        return sess.resolve_auto(path)

    @classmethod
    def open(cls, path: str | os.PathLike, mode: str = "auto") -> BoardApi:
        """Open the board at ``path``: headless preloads the file into a
        private KiCad 11 server, gui attaches to the PCB editor that has it."""
        kb.require(BoardApiError)
        target = os.path.abspath(str(path))
        if not target.lower().endswith(PCB_SUFFIX):
            raise BoardApiError(f"a board path must end with {PCB_SUFFIX}: {target}")
        if not Path(target).is_file():
            raise BoardApiError(f"{target} not found; build it first (build_board_from_netlist) "
                                "or check the path")
        session = sess.connect(mode, target)
        try:
            return cls(session, target)
        except BoardApiError:
            session.close()
            raise

    def close(self) -> None:
        """Release the session (stops a headless api-server); unsaved changes
        are lost in headless mode, left in the editor in gui mode."""
        if self._closed:
            return
        self._closed = True
        self._session.close()

    def discard(self) -> None:
        """Drop unsaved changes (headless: RevertDocument re-reads the file; a
        GUI document is left alone, its user owns the undo history) and close."""
        if not self._closed and self._dirty and self.mode == "headless":
            self._call("reverting", self._board.revert)
            self._dirty = False
        self.close()

    def __enter__(self) -> BoardApi:
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
    def session(self) -> sess.BoardSession:
        return self._session

    @property
    def is_modified(self) -> bool:
        """Copper added/removed or zones refilled since the last save()."""
        return self._dirty

    @property
    def connectivity_available(self) -> bool:
        """Whether the connected KiCad answers GetConnectedItems (KiCad >=
        10.0.1); settled by the version until a clusters() call proves
        otherwise."""
        if self._connectivity is None:
            self._connectivity = env.parse_version(self.kicad_version) >= CONNECTIVITY_MIN_VERSION
        return self._connectivity

    @property
    def notes(self) -> list[str]:
        """Degradations met so far (e.g. design rules this KiCad cannot report)."""
        return list(self._notes)

    # --- reads --------------------------------------------------------------
    def copper_layers(self) -> list[str]:
        """Enabled copper layers top to bottom: ['F.Cu', 'In1.Cu', 'In2.Cu',
        'B.Cu']. A KiCad before 9.0.5 has no GetBoardEnabledLayers: the
        file's (layers ...) table answers instead (noted)."""
        try:
            enabled = self._call("reading enabled layers", self._board.get_enabled_layers)
        except BoardApiError as exc:
            if not _unsupported(exc.__cause__):
                raise
            return self._file_copper_layers(exc)
        return items.copper_layer_names(enabled)

    def _file_copper_layers(self, exc: BoardApiError) -> list[str]:
        copper = file_copper_layers(self._path)
        if not copper:
            raise BoardApiError(
                f"{exc}; KiCad {self.kicad_version} predates GetBoardEnabledLayers (needs "
                f"{ENABLED_LAYERS_MIN_VERSION}) and {self._path} has no readable (layers ...) "
                "table: open the board through a headless KiCad 11 session (mode='headless')"
            ) from exc
        self._note(f"enabled layers unavailable on KiCad {self.kicad_version} (needs "
                   f"{ENABLED_LAYERS_MIN_VERSION}); copper layers read from the file's layer "
                   f"table: {copper}")
        return copper

    def nets(self) -> list[str]:
        """Sorted net names (the unnamed no-net is left out)."""
        return sorted(net.name for net in self._nets() if net.name)

    def pads(self, net: str = "", ref: str = "") -> list[dict]:
        """Pad dicts, optionally only one net's and/or one footprint's. On a
        KiCad before 10.0.6 pads report no parent: their reference comes from
        the footprint definitions, and a pad neither names is noted."""
        copper = self.copper_layers()
        refs = self._refs()
        raw = self._pads()
        homeless = [pad for pad in raw if pad.parent is None and str(pad.id.value) not in refs]
        if homeless:
            self._note(f"{len(homeless)} pad(s) carry no footprint reference: KiCad "
                       f"{self.kicad_version} reports no pad parents (needs "
                       f"{PAD_PARENT_MIN_VERSION}) and the footprint definitions do not list them")
        found = [items.pad_dict(pad, refs, copper) for pad in raw]
        return [pad for pad in found
                if (not net or pad["net"] == net) and (not ref or pad["ref"] == ref)]

    def tracks(self, net: str = "") -> list[dict]:
        found = [items.track_dict(t) for t in self._call("reading tracks", self._board.get_tracks)]
        return [track for track in found if not net or track["net"] == net]

    def vias(self, net: str = "") -> list[dict]:
        found = [items.via_dict(v) for v in self._call("reading vias", self._board.get_vias)]
        return [via for via in found if not net or via["net"] == net]

    def zones(self) -> list[dict]:
        return [items.zone_dict(z) for z in self._call("reading zones", self._board.get_zones)]

    def footprints(self) -> list[dict]:
        """Footprint dicts with KiCad's bounding box and pad count each (the
        count from the pads' parents, else from the footprint definition)."""
        counts = Counter(str(pad.parent.value) for pad in self._pads() if pad.parent is not None)
        out = []
        for fp in self._call("reading footprints", self._board.get_footprints):
            ref = fp.reference_field.text.value
            bbox = self._call(f"bounding box of {ref}", self._board.get_item_bounding_box, fp)
            pads = counts.get(str(fp.id.value), 0) or len(_definition_pads(fp))
            out.append(items.footprint_dict(fp, bbox, pads))
        return out

    def rules(self) -> dict:
        """Board minimums in mm (min_clearance, min_track_width, min_via_diameter,
        min_via_drill, min_via_annular, copper_edge_clearance, hole_clearance,
        hole_to_hole); every field None when this KiCad cannot report them
        (GetBoardDesignRules needs KiCad 11) and the reason lands in notes."""
        try:
            response = self._call("reading design rules", self._board.get_design_rules)
        except BoardApiError as exc:
            self._note(f"design rules unavailable, class fallbacks skip the board minimums: {exc}")
            return items.empty_rules()
        return items.rules_dict(response.rules.constraints)

    def netclasses(self) -> dict[str, dict]:
        """{net: {name, clearance, track_width, via_diameter, via_drill}} in
        mm for every named net: KiCad's resolved class, its None fields filled
        from the sibling .kicad_pro class of that name, then the board minimums."""
        nets = [net for net in self._nets() if net.name]
        if not nets:
            return {}
        found = self._call("reading net classes", self._board.get_netclass_for_nets, nets)
        project = project_netclasses(self._path)
        rules = self.rules()
        default = {"name": DEFAULT_CLASS, **{field: None for field in items.CLASS_FIELDS}}
        out = {}
        for net in nets:
            netclass = found.get(net.name)
            base = items.netclass_dict(netclass) if netclass is not None else default
            out[net.name] = _fill_class(base, project, rules)
        return dict(sorted(out.items()))

    def overview(self) -> dict:
        """Everything an agent needs before routing: {path, mode, kicad_version,
        copper_layers, outline, rules, netclasses, planes, footprints, counts}
        (+ notes when something degraded)."""
        copper = self.copper_layers()
        zones = self.zones()
        counts = {"pads": len(self._pads()),
                  "tracks": len(self._call("reading tracks", self._board.get_tracks)),
                  "vias": len(self._call("reading vias", self._board.get_vias)),
                  "zones": len(zones), "nets": len(self.nets())}
        shapes = self._call("reading shapes", self._board.get_shapes)
        out = {"path": self._path, "mode": self.mode, "kicad_version": self.kicad_version,
               "copper_layers": copper,
               "outline": self._convert("reading the outline", items.outline, shapes),
               "rules": self.rules(), "netclasses": self.netclasses(),
               "planes": [zone for zone in zones if _is_plane(zone, copper)],
               "footprints": self.footprints(), "counts": counts}
        return {**out, "notes": self.notes} if self._notes else out

    def clusters(self, net: str) -> list[list[str]]:
        """Connected groups of the net's pads as sorted pad-id lists, from
        KiCad's real connectivity (tracks, vias AND filled zones: two pads on
        one plane share a cluster). One cluster = the net is routed; [] = no
        pads. Raises BoardApiError("connectivity unavailable ...") on a KiCad
        that lacks GetConnectedItems."""
        self._net(net)
        pads = [pad for pad in self._pads() if pad.net.name == net]
        parent: dict[str, str] = {}
        for pad in pads:
            for item in self._connected(pad):
                _union(parent, str(pad.id.value), str(item.id.value))
        groups: dict[str, list[str]] = {}
        for pad in pads:
            groups.setdefault(_find(parent, str(pad.id.value)), []).append(str(pad.id.value))
        return sorted(sorted(ids) for ids in groups.values())

    # --- writes -------------------------------------------------------------
    def add_tracks(self, net: str, segments: Iterable[dict]) -> list[dict]:
        """Lay straight copper on ``net``: each segment {layer, start: [x, y],
        end: [x, y], width} in mm on one of the board's copper layers.
        Returns the created track dicts (with KiCad's ids)."""
        net_obj = self._net(net)
        copper = self.copper_layers()
        built = [items.track_item(net_obj, *_check_segment(seg, copper)) for seg in segments]
        if not built:
            raise BoardApiError("add_tracks: give at least one segment")
        return [items.track_dict(track) for track in self._create(built)]

    def add_vias(self, net: str, vias: Iterable[dict]) -> list[dict]:
        """Through vias on ``net``: each {x, y, diameter, drill} in mm.
        Returns the created via dicts (with KiCad's ids)."""
        net_obj = self._net(net)
        built = [items.via_item(net_obj, *_check_via(spec)) for spec in vias]
        if not built:
            raise BoardApiError("add_vias: give at least one via")
        return [items.via_dict(via) for via in self._create(built)]

    def remove(self, ids: Iterable[str]) -> int:
        """Remove tracks/vias by id (from tracks()/vias()/add_*); an id that
        is not a track or via on this board is refused, nothing removed."""
        wanted = [str(ident) for ident in ids]
        if not wanted:
            raise BoardApiError("remove: give at least one track or via id")
        known = {str(item.id.value): item for item in self._routing()}
        unknown = [ident for ident in wanted if ident not in known]
        if unknown:
            raise BoardApiError(f"not a track or via on this board: {unknown} (ids come from "
                                "tracks(), vias(), add_tracks() or add_vias())")
        self._call("removing items", self._board.remove_items, [known[i] for i in wanted])
        self._dirty = True
        return len(wanted)

    def remove_net_routing(self, net: str) -> dict:
        """Strip every track and via of ``net``; {"tracks": n, "vias": m} removed."""
        self._net(net)
        doomed = [item for item in self._routing() if items.net_name(item.net) == net]
        tracks = [item for item in doomed if not isinstance(item, kb.Via)]
        vias = [item for item in doomed if isinstance(item, kb.Via)]
        if doomed:
            self._call(f"removing the routing of {net}", self._board.remove_items, doomed)
            self._dirty = True
        return {"tracks": len(tracks), "vias": len(vias)}

    def refill_zones(self) -> int:
        """Refill every zone (blocks until KiCad is done); the zone count."""
        self._call("refilling zones", self._board.refill_zones)
        self._dirty = True
        return len(self._call("reading zones", self._board.get_zones))

    def save(self) -> str:
        """Write the board in place (KiCad 11 format from a headless session:
        see the module doc) and clear the modified flag."""
        self._call("saving", self._board.save)
        self._dirty = False
        return self._path

    def save_as(self, path: str | os.PathLike, overwrite: bool = True) -> str:
        """Write a copy (with its project) to ``path``; the open board keeps
        its own path and its modified flag."""
        target = os.path.abspath(str(path))
        if Path(target).exists() and not overwrite:
            raise BoardApiError(f"{target} exists; pass overwrite=True to replace it")
        self._call(f"saving a copy to {target}", self._board.save_as, target, overwrite)
        return target

    # --- library footprints -------------------------------------------------
    def place_footprint(self, lib_id: str, ref: str, x_mm: float, y_mm: float,
                        rot: float = 0, side: str = "F", value: str = "") -> dict:
        """Place library footprint ``lib_id`` ('<nickname>:<footprint>': a row
        of the project fp-lib-table that existed when the board was opened, or
        a stock library) with its origin at (x_mm, y_mm), rotated ``rot``
        degrees, on side 'F' or 'B' (KiCad flips it), as ``ref`` with
        ``value`` ('' keeps the library's, the footprint name). The footprint
        libraries load first (headless: once per session). Returns {id, ref,
        value, x, y, rot, side, bbox, pads (count), lib_id (KiCad's FPID)};
        the pads have no nets yet. Every library error names the lib_id and
        the project fp-lib-table."""
        self._require_open()
        _check_lib_id(lib_id, Path(self._path).parent / sess.FP_TABLE)
        _check_ref(ref)
        if not isinstance(value, str):
            raise BoardApiError(f"value must be a string, got {value!r}")
        position = kb.vec(_coord(x_mm, "x"), _coord(y_mm, "y"))
        angle, layer = _angle(_degrees(rot)), _side_layer(side)
        self._refuse_duplicate(ref)
        self._session.load_libraries((kb.LT_FOOTPRINT,))
        placed = self._place(lib_id, ref, position, angle, layer)
        self._dirty = True                       # KiCad holds the footprint from here on
        placed.reference_field.text.value = ref
        if value:
            placed.value_field.text.value = value
        updated = self._call(f"naming the placed {lib_id} {ref} (the board now holds it "
                             "unnamed, unsaved; discard() drops it)",
                             self._board.update_items, [placed])
        footprint = updated[0] if updated else placed
        bbox = self._call(f"bounding box of {ref}", self._board.get_item_bounding_box, footprint)
        fpid = str(footprint.definition.id)
        return {**items.footprint_dict(footprint, bbox, len(_definition_pads(footprint))),
                "lib_id": fpid if fpid.strip(":") else lib_id}

    def _refuse_duplicate(self, ref: str) -> None:
        taken = {str(fp.reference_field.text.value)
                 for fp in self._call("reading footprints", self._board.get_footprints)}
        if ref in taken:
            raise BoardApiError(f"reference {ref} is already used on {self._path}; pick a free "
                                "one (footprints() lists the board's references)")

    def _place(self, lib_id: str, ref: str, position: kb.Vector2, angle: Any,
               layer: int) -> kb.FootprintInstance:
        try:
            return self._board.place_footprint_from_library(lib_id, position, angle, layer)
        except kb.KIPY_EXCEPTIONS as exc:
            hint = self._session.footprint_library_hint(lib_id.partition(":")[0])
            raise BoardApiError(f"placing {lib_id} as {ref} on {self._path} failed: {exc}; "
                                f"{hint}") from exc

    # --- internals ----------------------------------------------------------
    def _require_open(self) -> None:
        if self._closed:
            raise BoardApiError(f"{self._path} is closed; open it again with BoardApi.open")

    def _call(self, what: str, fn, *args, **kwargs) -> Any:
        self._require_open()
        try:
            return fn(*args, **kwargs)
        except kb.KIPY_EXCEPTIONS as exc:
            raise BoardApiError(f"{what} failed: {exc}") from exc

    def _convert(self, what: str, fn: Callable[..., Any], *args) -> Any:
        """Run a pure converter over kipy items; a surprise inside the vendored
        wrappers (a shape kind they cannot box) becomes BoardApiError so the
        tool answers instead of raising out of the MCP server."""
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 - the reason is reported, never swallowed
            raise BoardApiError(f"{what} failed: {type(exc).__name__}: {exc}") from exc

    def _note(self, message: str) -> None:
        if message not in self._notes:
            self._notes.append(message)

    def _nets(self) -> list:
        return list(self._call("reading nets", self._board.get_nets))

    def _net(self, name: str) -> kb.Net:
        """The board's Net named ``name`` (never a fresh Net: copper on an
        unknown net would be electrically dead)."""
        if not isinstance(name, str) or not name.strip():
            raise BoardApiError(f"net name must be a non-empty string, got {name!r}")
        for net in self._nets():
            if net.name == name:
                return net
        raise BoardApiError(f"no net {name!r} on {self._path}; nets: {self.nets()}")

    def _pads(self) -> list:
        return list(self._call("reading pads", self._board.get_pads))

    def _refs(self) -> dict[str, str]:
        """footprint KIID -> reference, plus pad KIID -> reference from each
        footprint's definition (the parent link a KiCad before 10.0.6 lacks)."""
        out: dict[str, str] = {}
        for fp in self._call("reading footprints", self._board.get_footprints):
            ref = str(fp.reference_field.text.value)
            out[str(fp.id.value)] = ref
            for pad in _definition_pads(fp):
                out[str(pad.id.value)] = ref
        return out

    def _routing(self) -> list:
        tracks = self._call("reading tracks", self._board.get_tracks)
        vias = self._call("reading vias", self._board.get_vias)
        return list(tracks) + list(vias)

    def _connected(self, pad) -> list:
        """Copper items connected to ``pad`` (KiCad includes the pad itself).
        Only a KiCad that has NO handler for GetConnectedItems settles
        ``connectivity_available`` to False; any other failure (a timeout on
        a busy KiCad, a dropped socket) is reported and leaves it as it was."""
        self._require_open()
        try:
            found = self._board.get_connected_items(pad)
        except kb.KIPY_EXCEPTIONS as exc:
            if not _unsupported(exc):
                raise BoardApiError(
                    f"connectivity of pad {pad.number} ({items.net_name(pad.net)}) failed: "
                    f"{exc}; retry the call (a busy KiCad answers late)") from exc
            self._connectivity = False
            raise BoardApiError(
                f"connectivity unavailable on KiCad {self.kicad_version} ({exc}); "
                "GetConnectedItems needs KiCad >= 10.0.1: open the board through a headless "
                "KiCad 11 session (mode='headless') or use kicad-cli DRC's unconnected items"
            ) from exc
        self._connectivity = True
        return list(found)

    def _create(self, new_items: list) -> list:
        created = self._call("creating items", self._board.create_items, new_items)
        if len(created) != len(new_items):
            raise BoardApiError(
                f"KiCad created {len(created)} of {len(new_items)} items on {self._path}")
        self._dirty = True
        return list(created)
