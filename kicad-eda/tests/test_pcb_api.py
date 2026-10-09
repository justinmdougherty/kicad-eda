"""backends.pcb_api — the KiCad IPC-API board backend.

Two tiers in one module:

* pure tests over hand-built fakes (mode selection, converters, the
  cluster union-find, track/via construction, validation, the dirty flag,
  discard) that run wherever the vendored kipy imports (no KiCad needed);
* live tests against the KiCad 11 nightly's headless ``kicad-cli api-server``,
  skipped when ``BoardApi.available()`` is False or no KiCad 9/10 pcbnew
  Python can build the unrouted example board. Every session a live test
  opens is closed before the test returns and the tests assert that no
  kicad-cli.exe outlives them.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import board_pipeline_tools
import toolchain as tc
from backends import kipy_bridge as kb
from backends import pcb_api as papi
from backends import pcb_items as items
from backends import pcb_session as sess
from backends.errors import BoardApiError
from backends.pcb_api import BoardApi
from fixtures.kicad_procs import owned_pids

requires_api = pytest.mark.skipif(not BoardApi.available(),
                                  reason=BoardApi.unavailable_reason() or "")
requires_kipy = pytest.mark.skipif(bool(kb.IMPORT_ERROR), reason=kb.IMPORT_ERROR or "")
ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
KICAD9_FORMAT = 20241229                     # (version ...) of a KiCad 9 board file
KICAD9_LAYER_TABLE = """(kicad_pcb
\t(version 20241229)
\t(generator "pcbnew")
\t(generator_version "9.0")
\t(general (thickness 1.6))
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(4 "In1.Cu" signal)
\t\t(6 "In2.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(9 "F.Adhes" user "F.Adhesive")
\t\t(44 "Edge.Cuts" user)
\t)
\t(setup (pad_to_mask_clearance 0))
"""


# ============================================================================
# fakes (attributes mirror the kipy wrappers pcb_items reads)
# ============================================================================
def vec(x_mm: float, y_mm: float) -> SimpleNamespace:
    return SimpleNamespace(x=int(round(x_mm * 1e6)), y=int(round(y_mm * 1e6)))


def kiid(value: str) -> SimpleNamespace:
    return SimpleNamespace(value=value)


def net(name: str):
    return kb.Net(name=name)                 # a real Net: Track.net / Via.net copy its proto


def box(x0: float, y0: float, x1: float, y1: float) -> SimpleNamespace:
    return SimpleNamespace(pos=vec(x0, y0), size=vec(x1 - x0, y1 - y0))


def pad(ident: str, parent: str | None, number: str, net_name: str, x: float, y: float, *,
        smd: bool = True, w: float = 1.0, h: float = 0.5, angle: float = 0.0,
        drill: float = 0.0, shape: str = "PSS_ROUNDRECT", side: str = "F.Cu") -> SimpleNamespace:
    """``parent`` None = a pad from a KiCad before 10.0.6 (no parent link)."""
    layer = items.layer_value
    if smd:
        layers = [layer(side), layer("F.Mask"), layer("F.Paste")]
    else:                                    # KiCad lists every inner layer it knows
        layers = [layer(n) for n in ("F.Cu", "B.Cu", "In1.Cu", "In2.Cu", "In17.Cu")]
    stack = SimpleNamespace(
        copper_layers=[SimpleNamespace(layer=layer("F.Cu"), shape=kb.PadStackShape.Value(shape),
                                       size=vec(w, h))],
        layers=layers, drill=SimpleNamespace(diameter=vec(drill, drill)),
        angle=SimpleNamespace(degrees=angle))
    return SimpleNamespace(id=kiid(ident), parent=kiid(parent) if parent is not None else None,
                           number=number, net=net(net_name), position=vec(x, y),
                           pad_type=kb.PadType.PT_SMD if smd else kb.PadType.PT_PTH,
                           padstack=stack)


def track(ident: str, net_name: str, layer: str, x1: float, y1: float, x2: float, y2: float,
          width: float = 0.25) -> SimpleNamespace:
    return SimpleNamespace(id=kiid(ident), net=net(net_name), layer=items.layer_value(layer),
                           start=vec(x1, y1), end=vec(x2, y2), width=int(round(width * 1e6)))


def arc(ident: str, net_name: str, layer: str, start: tuple, mid: tuple, end: tuple,
        length_mm: float) -> SimpleNamespace:
    base = track(ident, net_name, layer, *start, *end)
    return SimpleNamespace(**vars(base), mid=vec(*mid), length=lambda: length_mm * 1e6)


def via(ident: str, net_name: str, x: float, y: float, diameter: float = 0.6,
        drill: float = 0.3):
    item = kb.Via()                          # a real Via: remove_net_routing sorts by type
    item.position = kb.vec(x, y)
    item.diameter = items.nm(diameter)
    item.drill_diameter = items.nm(drill)
    item.net = net(net_name)
    item.id.value = ident
    return item


def zone(ident: str, name: str, net_name: str, layers: list[str], bbox: tuple, *,
         filled: bool = True, rule_area: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=kiid(ident), name=name, net=net(net_name) if net_name else None,
                           layers=[items.layer_value(layer) for layer in layers],
                           filled=filled, is_rule_area=lambda: rule_area,
                           bounding_box=lambda: box(*bbox))


def footprint(ident: str, ref: str, value: str, x: float, y: float, rot: float = 0.0,
              layer: str = "F.Cu", pad_ids: tuple[str, ...] = ()) -> SimpleNamespace:
    """``pad_ids`` = the pads KiCad serialises inside the footprint definition."""
    field = lambda text: SimpleNamespace(text=SimpleNamespace(value=text))  # noqa: E731
    definition = SimpleNamespace(pads=[SimpleNamespace(id=kiid(i)) for i in pad_ids])
    return SimpleNamespace(id=kiid(ident), reference_field=field(ref), value_field=field(value),
                           position=vec(x, y), orientation=SimpleNamespace(degrees=rot),
                           layer=items.layer_value(layer), definition=definition)


def edge(x0: float, y0: float, x1: float, y1: float, layer: str = "Edge.Cuts") -> SimpleNamespace:
    return SimpleNamespace(layer=items.layer_value(layer),
                           bounding_box=lambda: box(min(x0, x1), min(y0, y1),
                                                    max(x0, x1), max(y0, y1)))


def design_rules(**override) -> SimpleNamespace:
    base = {"min_clearance": 150000, "min_track_width": 150000, "min_via_size": 450000,
            "min_through_drill": 200000, "min_via_annular_width": 100000,
            "copper_edge_clearance": 300000, "hole_clearance": 250000, "hole_to_hole_min": 250000}
    constraints = SimpleNamespace(**{**base, **override})
    return SimpleNamespace(rules=SimpleNamespace(constraints=constraints))


def netclass(name: str = "Default", clearance=200000, track_width=200000,
             via_diameter=600000, via_drill=300000) -> SimpleNamespace:
    return SimpleNamespace(name=name, clearance=clearance, track_width=track_width,
                           via_diameter=via_diameter, via_drill=via_drill)


class FakeBoard:
    """Records every call and hands back the hand-built items it was given."""
    COPPER = ("F.Cu", "B.Cu", "In1.Cu", "In2.Cu")   # KiCad's enum order, not stackup order

    def __init__(self, *, pads=(), tracks=(), vias=(), zones=(), footprints=(), shapes=(),
                 nets=(), connections=None, classes=None, rules=None, bboxes=None,
                 connectivity=True, enabled_layers=None) -> None:
        """``connectivity``: True, False (KiCad has no GetConnectedItems) or an
        exception to raise; ``enabled_layers``: an exception to raise from
        get_enabled_layers (a KiCad before 9.0.5)."""
        self.calls: list[str] = []
        self.pads, self.tracks, self.vias = list(pads), list(tracks), list(vias)
        self.zones, self.footprints, self.shapes = list(zones), list(footprints), list(shapes)
        self.nets = [net(name) for name in nets]
        self.connections = dict(connections or {})
        self.classes = dict(classes or {})
        self.rules = rules if rules is not None else design_rules()
        self.bboxes = dict(bboxes or {})
        self.connectivity = connectivity
        self.enabled_layers = enabled_layers
        self.created: list = []
        self.removed: list = []

    def _rec(self, name: str) -> None:
        self.calls.append(name)

    def get_enabled_layers(self):
        self._rec("get_enabled_layers")
        if isinstance(self.enabled_layers, BaseException):
            raise self.enabled_layers
        return [items.layer_value(n) for n in self.COPPER] + [items.layer_value("Edge.Cuts")]

    def get_nets(self):
        self._rec("get_nets")
        return list(self.nets)

    def get_pads(self):
        self._rec("get_pads")
        return list(self.pads)

    def get_footprints(self):
        self._rec("get_footprints")
        return list(self.footprints)

    def get_tracks(self):
        self._rec("get_tracks")
        return list(self.tracks)

    def get_vias(self):
        self._rec("get_vias")
        return list(self.vias)

    def get_zones(self):
        self._rec("get_zones")
        return list(self.zones)

    def get_shapes(self):
        self._rec("get_shapes")
        return list(self.shapes)

    def get_netclass_for_nets(self, nets):
        self._rec("get_netclass_for_nets")
        return dict(self.classes)

    def get_design_rules(self):
        self._rec("get_design_rules")
        if isinstance(self.rules, Exception):
            raise self.rules
        return self.rules

    def get_item_bounding_box(self, item):
        self._rec("get_item_bounding_box")
        return self.bboxes.get(item.id.value)

    def get_connected_items(self, item):
        self._rec("get_connected_items")
        if isinstance(self.connectivity, BaseException):
            raise self.connectivity
        if not self.connectivity:                # what a KiCad without the handler answers
            raise kb.ApiError("no handler available for request of type GetConnectedItems",
                              code=kb.ApiStatusCode.AS_UNHANDLED)
        return [item] + list(self.connections.get(item.id.value, []))

    def create_items(self, new_items):
        self._rec("create_items")
        self.created.extend(new_items)
        return list(new_items)

    def remove_items(self, doomed):
        self._rec("remove_items")
        ids = {str(item.id.value) for item in doomed}
        self.removed.extend(doomed)
        self.tracks = [t for t in self.tracks if str(t.id.value) not in ids]
        self.vias = [v for v in self.vias if str(v.id.value) not in ids]

    def refill_zones(self):
        self._rec("refill_zones")

    def save(self):
        self._rec("save")

    def save_as(self, path, overwrite):
        self._rec(f"save_as:{overwrite}")

    def revert(self):
        self._rec("revert")


class FakeSession:
    def __init__(self, board: FakeBoard, mode: str = "headless",
                 version: str = "10.99.0-4025-gf7ca62") -> None:
        self._board = board
        self.mode = mode
        self.kicad_version = version
        self.closed = False

    def board(self, path):
        return self._board

    def close(self):
        self.closed = True


def api_over(board: FakeBoard, tmp_path: Path, mode: str = "headless",
             version: str = "10.99.0-4025-gf7ca62") -> BoardApi:
    return BoardApi(FakeSession(board, mode, version), str(tmp_path / "b.kicad_pcb"))


def two_pad_board(**extra) -> FakeBoard:
    """A1 and A2 on OUT_A, G1 on GND, one footprint each."""
    pads = [pad("pA1", "fpU1", "1", "OUT_A", 10, 10), pad("pA2", "fpC1", "1", "OUT_A", 14, 10),
            pad("pG1", "fpC1", "2", "GND", 14, 12, smd=False, drill=0.8, shape="PSS_CIRCLE",
                w=1.6, h=1.6)]
    footprints = [footprint("fpU1", "U1", "LM358", 10, 10), footprint("fpC1", "C1", "100n", 14, 11)]
    return FakeBoard(pads=pads, footprints=footprints, nets=("OUT_A", "GND", ""), **extra)


# ============================================================================
# pure tier
# ============================================================================
def test_select_mode_decisions(tmp_path):
    path = tmp_path / "a.kicad_pcb"
    here = [sess.norm_path(path)]
    assert sess.select_mode("headless", path, here) == "headless"
    assert sess.select_mode("auto", path, None) == "headless"
    assert sess.select_mode("auto", path, [sess.norm_path(tmp_path / "b.kicad_pcb")]) == "headless"
    assert sess.select_mode("auto", path, here) == "gui"
    assert sess.select_mode("gui", path, here) == "gui"
    with pytest.raises(BoardApiError, match="no KiCad GUI"):
        sess.select_mode("gui", path, None)
    with pytest.raises(BoardApiError, match="not open in the running KiCad PCB editor"):
        sess.select_mode("gui", path, [])
    with pytest.raises(BoardApiError, match="mode must be one of"):
        sess.select_mode("remote", path, None)


def test_doc_pcb_path_joins_relative_board_filename(tmp_path):
    project = SimpleNamespace(path=str(tmp_path), name="board")
    relative = SimpleNamespace(board_filename="board.kicad_pcb", project=project)
    assert sess.doc_pcb_path(relative) == str(tmp_path / "board.kicad_pcb")
    absolute = SimpleNamespace(board_filename=str(tmp_path / "x.kicad_pcb"), project=project)
    assert sess.doc_pcb_path(absolute) == str(tmp_path / "x.kicad_pcb")
    assert sess.doc_pcb_path(SimpleNamespace(board_filename="", project=project)) == \
        str(tmp_path / "board.kicad_pcb")
    assert sess.doc_pcb_path(SimpleNamespace()) == ""


@requires_kipy
def test_open_board_paths_with_fake_kicad(tmp_path):
    docs = [SimpleNamespace(board_filename="one.kicad_pcb",
                            project=SimpleNamespace(path=str(tmp_path), name="one")),
            SimpleNamespace(board_filename="", project=SimpleNamespace(path="", name=""))]
    fake = SimpleNamespace(get_open_documents=lambda _t: docs)
    assert sess.open_board_paths(fake) == [sess.norm_path(tmp_path / "one.kicad_pcb")]

    def refuse(_t):
        raise kb.ApiError("no handler available")
    assert sess.open_board_paths(SimpleNamespace(get_open_documents=refuse)) == []
    assert sess.open_boards(SimpleNamespace(get_open_documents=refuse)) == []

    def broken(_t):
        raise kb.ApiError("editor busy")
    with pytest.raises(BoardApiError, match="listing open boards failed"):
        sess.open_boards(SimpleNamespace(get_open_documents=broken))


@requires_kipy
def test_connect_auto_falls_back_when_no_gui_answers(tmp_path, monkeypatch):
    """kipy dials lazily, so KiCad() succeeds even when nothing listens: the
    GUI probe must verify the connection and 'auto' must go headless."""
    class DeadKiCad:
        closed = False

        def __init__(self, **_kw):
            DeadKiCad.closed = False

        def get_version(self):
            raise kb.KiCadConnectionError("Failed to connect to KiCad: Connection refused")

        def close(self):
            DeadKiCad.closed = True

    headless = SimpleNamespace(mode="headless")
    monkeypatch.setattr(kb, "KiCad", DeadKiCad)
    monkeypatch.setattr(sess.BoardSession, "headless",
                        classmethod(lambda cls, path, **_kw: headless))
    assert sess.BoardSession.gui() is None and DeadKiCad.closed is True
    assert sess.connect("auto", str(tmp_path / "a.kicad_pcb")) is headless
    with pytest.raises(BoardApiError, match="no KiCad GUI answers"):
        sess.connect("gui", str(tmp_path / "a.kicad_pcb"))
    with pytest.raises(BoardApiError, match="mode must be one of"):
        sess.connect("remote", str(tmp_path / "a.kicad_pcb"))


@requires_kipy
def test_layer_names_round_trip():
    layer = kb.BoardLayer
    assert items.layer_name(layer.BL_F_Cu) == "F.Cu"
    assert items.layer_name(layer.BL_Edge_Cuts) == "Edge.Cuts"
    assert items.layer_value("In1.Cu") == layer.BL_In1_Cu
    assert items.layer_value("B.Cu") == layer.BL_B_Cu
    assert items.layer_value("Edge.Cuts") == layer.BL_Edge_Cuts
    with pytest.raises(ValueError, match="F.Cu"):
        items.layer_value("Top")
    with pytest.raises(ValueError, match="unknown layer"):
        items.layer_value("UNKNOWN")
    enum_order = [layer.BL_F_Cu, layer.BL_B_Cu, layer.BL_In1_Cu, layer.BL_In2_Cu, layer.BL_Edge_Cuts]
    assert items.copper_layer_names(enum_order) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert items.nm(0.35) == 350000 and items.mm(349999) == 0.35


@requires_kipy
def test_pad_dict_smd_vs_pth():
    copper = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    refs = {"fpU1": "U1", "fpJ1": "J1"}
    smd = items.pad_dict(pad("p1", "fpU1", "3", "OUT_A", 10.5, 20.25, w=0.9, h=0.95, angle=90),
                         refs, copper)
    assert smd == {"id": "p1", "ref": "U1", "pad": "3", "net": "OUT_A", "x": 10.5, "y": 20.25,
                   "layers": ["F.Cu"], "through_hole": False, "shape": "roundrect",
                   "w": 0.9, "h": 0.95, "rotation": 90.0, "drill": None}
    bottom = items.pad_dict(pad("p2", "fpU1", "4", "OUT_A", 1, 1, side="B.Cu"), refs, copper)
    assert bottom["layers"] == ["B.Cu"]
    pth = items.pad_dict(pad("p3", "fpJ1", "1", "GND", 3, 4, smd=False, drill=1.0,
                             shape="PSS_RECTANGLE", w=1.7, h=1.7), refs, copper)
    assert pth["layers"] == copper and pth["through_hole"] is True
    assert (pth["shape"], pth["drill"], pth["w"]) == ("rect", 1.0, 1.7)
    orphan = items.pad_dict(pad("p4", "nobody", "1", "", 0, 0), refs, copper)
    assert (orphan["ref"], orphan["net"]) == ("", "")


@requires_kipy
def test_track_via_zone_footprint_dicts():
    straight = items.track_dict(track("t1", "OUT_A", "F.Cu", 10, 10, 13, 14, width=0.25))
    assert straight == {"id": "t1", "net": "OUT_A", "layer": "F.Cu", "start": [10.0, 10.0],
                        "end": [13.0, 14.0], "width": 0.25, "length": 5.0}
    curved = items.track_dict(arc("t2", "OUT_A", "B.Cu", (0, 0), (1, 1), (2, 0), 3.1416))
    assert curved["arc"] is True and curved["mid"] == [1.0, 1.0] and curved["length"] == 3.1416
    assert "arc" not in straight
    assert items.via_dict(via("v1", "GND", 5, 6)) == {"id": "v1", "net": "GND", "x": 5.0,
                                                       "y": 6.0, "diameter": 0.6, "drill": 0.3}
    plane = items.zone_dict(zone("z1", "gnd", "GND", ["In1.Cu"], (0, 0, 30, 25)))
    assert plane == {"id": "z1", "name": "gnd", "net": "GND", "layers": ["In1.Cu"],
                     "filled": True, "rule_area": False, "bbox": [0.0, 0.0, 30.0, 25.0]}
    keepout = items.zone_dict(zone("z2", "ko", "", ["F.Cu"], (1, 1, 2, 2), rule_area=True))
    assert keepout["net"] == "" and keepout["rule_area"] is True
    fp = items.footprint_dict(footprint("f1", "U1", "LM358", 10, 20, 90, "B.Cu"),
                              box(8, 18, 12, 22), 8)
    assert fp == {"id": "f1", "ref": "U1", "value": "LM358", "x": 10.0, "y": 20.0, "rot": 90.0,
                  "side": "B", "bbox": [8.0, 18.0, 12.0, 22.0], "pads": 8}
    assert items.footprint_dict(footprint("f2", "C1", "1u", 0, 0), None, 2)["bbox"] is None


@requires_kipy
def test_outline_uses_edge_cuts_only():
    shapes = [edge(0, 0, 30, 0), edge(30, 0, 30, 25), edge(30, 25, 0, 25), edge(0, 25, 0, 0),
              edge(-5, -5, 40, 40, layer="F.SilkS")]
    assert items.outline(shapes) == {"bbox": [0.0, 0.0, 30.0, 25.0], "width": 30.0,
                                     "height": 25.0, "segments": 4}
    assert items.outline([]) == {"bbox": None, "width": 0.0, "height": 0.0, "segments": 0}


@requires_kipy
def test_outline_boxes_a_bezier_by_its_control_polygon_and_skips_what_it_cannot_box():
    def unboxable():
        raise NotImplementedError()

    bezier = SimpleNamespace(layer=items.layer_value("Edge.Cuts"), bounding_box=unboxable,
                             start=vec(0, 0), control1=vec(2, -3), control2=vec(8, -3),
                             end=vec(10, 0))
    ellipse = SimpleNamespace(layer=items.layer_value("Edge.Cuts"))   # a bare BoardShape
    shapes = [edge(0, 0, 10, 0), edge(10, 0, 10, 5), bezier, ellipse]
    assert items.outline(shapes) == {"bbox": [0.0, -3.0, 10.0, 5.0], "width": 10.0,
                                     "height": 8.0, "segments": 4, "skipped": 1}
    assert items.outline([ellipse]) == {"bbox": None, "width": 0.0, "height": 0.0,
                                        "segments": 1, "skipped": 1}


@requires_kipy
def test_outline_survives_kipys_own_bezier_and_ellipse_wrappers():
    """The vendored wrappers as the api-server hands them out: BoardBezier
    (bounding_box raises NotImplementedError) and a bare BoardShape for an
    ellipse (to_concrete_board_shape has no class for that geometry)."""
    from kipy.board_types import BoardBezier, BoardShape, to_concrete_board_shape
    curve = BoardBezier()
    curve.layer = kb.BoardLayer.BL_Edge_Cuts
    curve.start, curve.control1 = kb.vec(0, 0), kb.vec(1, -2)
    curve.control2, curve.end = kb.vec(4, -2), kb.vec(5, 0)
    raw = BoardShape()
    raw.layer = kb.BoardLayer.BL_Edge_Cuts
    raw.proto.shape.ellipse.SetInParent()
    ellipse = to_concrete_board_shape(raw)
    with pytest.raises(NotImplementedError):
        curve.bounding_box()
    assert not hasattr(ellipse, "bounding_box")
    assert items.outline([curve, ellipse]) == {"bbox": [0.0, -2.0, 5.0, 0.0], "width": 5.0,
                                               "height": 2.0, "segments": 2, "skipped": 1}


@requires_kipy
def test_overview_reports_a_converter_surprise_as_board_api_error(tmp_path):
    def broken():
        raise RuntimeError("kipy could not box this shape")

    odd = SimpleNamespace(layer=items.layer_value("Edge.Cuts"), bounding_box=broken)
    api = api_over(two_pad_board(shapes=[odd]), tmp_path)
    with pytest.raises(BoardApiError, match="reading the outline failed: RuntimeError: kipy "
                                            "could not box"):
        api.overview()


@requires_kipy
def test_rules_and_netclass_dicts_tolerate_missing_fields():
    rules = items.rules_dict(design_rules().rules.constraints)
    assert rules == {"min_clearance": 0.15, "min_track_width": 0.15, "min_via_diameter": 0.45,
                     "min_via_drill": 0.2, "min_via_annular": 0.1, "copper_edge_clearance": 0.3,
                     "hole_clearance": 0.25, "hole_to_hole": 0.25}
    partial = items.rules_dict(SimpleNamespace(min_clearance=100000))
    assert partial["min_clearance"] == 0.1 and partial["hole_to_hole"] is None
    assert items.netclass_dict(netclass("Power", track_width=None)) == {
        "name": "Power", "clearance": 0.2, "track_width": None, "via_diameter": 0.6,
        "via_drill": 0.3}


@requires_kipy
def test_clusters_join_pads_through_a_track(tmp_path):
    link = track("t1", "OUT_A", "F.Cu", 10, 10, 14, 10)
    board = two_pad_board(tracks=[link], connections={"pA1": [link], "pA2": [link]})
    api = api_over(board, tmp_path)
    assert api.clusters("OUT_A") == [["pA1", "pA2"]]
    assert api.clusters("GND") == [["pG1"]]
    assert api.connectivity_available is True
    board.connections = {}
    assert api.clusters("OUT_A") == [["pA1"], ["pA2"]]
    with pytest.raises(BoardApiError, match="no net 'NOPE'.*nets: \\['GND', 'OUT_A'\\]"):
        api.clusters("NOPE")


@requires_kipy
def test_clusters_join_pads_through_a_zone(tmp_path):
    plane = zone("z1", "", "SIG", ["In1.Cu"], (0, 0, 30, 25))
    pads = [pad("p1", "f", "1", "SIG", 1, 1), pad("p2", "f", "2", "SIG", 2, 2),
            pad("p3", "f", "3", "SIG", 3, 3)]
    board = FakeBoard(pads=pads, zones=[plane], nets=("SIG",),
                      connections={"p1": [plane], "p2": [plane]})
    assert api_over(board, tmp_path).clusters("SIG") == [["p1", "p2"], ["p3"]]


@requires_kipy
def test_clusters_report_connectivity_unavailable(tmp_path):
    board = two_pad_board(connectivity=False)
    api = api_over(board, tmp_path, mode="gui", version="9.0.4")
    assert api.connectivity_available is False           # by version, before any call
    with pytest.raises(BoardApiError, match="connectivity unavailable on KiCad 9.0.4"):
        api.clusters("OUT_A")
    assert api.connectivity_available is False
    modern = api_over(two_pad_board(connectivity=False), tmp_path)
    assert modern.connectivity_available is True         # 10.99 claims it ...
    with pytest.raises(BoardApiError, match="connectivity unavailable"):
        modern.clusters("OUT_A")
    assert modern.connectivity_available is False        # ... until KiCad refuses


@requires_kipy
def test_clusters_transient_failure_does_not_latch_connectivity_off(tmp_path):
    """A timeout or a busy editor is a failed call, not a missing handler: it
    is reported and the routed gate stays available for the next call."""
    board = two_pad_board(connectivity=TimeoutError("KiCad answered nothing in 60 s"))
    api = api_over(board, tmp_path)
    with pytest.raises(BoardApiError, match=r"connectivity of pad 1 \(OUT_A\) failed: KiCad "
                                            r"answered nothing in 60 s; retry"):
        api.clusters("OUT_A")
    assert api.connectivity_available is True
    board.connectivity = kb.ApiError("editor busy", code=kb.ApiStatusCode.AS_BUSY)
    with pytest.raises(BoardApiError, match="failed: editor busy"):
        api.clusters("OUT_A")
    assert api.connectivity_available is True
    board.connectivity = True
    assert api.clusters("OUT_A") == [["pA1"], ["pA2"]]      # the next call simply works
    board.connectivity = kb.ApiError("no handler available for request",
                                     code=kb.ApiStatusCode.AS_BAD_REQUEST)
    with pytest.raises(BoardApiError, match="connectivity unavailable"):
        api.clusters("OUT_A")                                # the no-handler text alone settles it
    assert api.connectivity_available is False


@requires_kipy
def test_copper_layers_fall_back_to_the_file_layer_table_before_kicad_9_0_5(tmp_path):
    unhandled = kb.ApiError("no handler available for request of type GetBoardEnabledLayers",
                            code=kb.ApiStatusCode.AS_UNHANDLED)
    api = api_over(two_pad_board(enabled_layers=unhandled), tmp_path, mode="gui", version="9.0.4")
    with pytest.raises(BoardApiError, match="needs 9.0.5.*mode='headless'"):
        api.copper_layers()                                  # no file to read yet
    (tmp_path / "b.kicad_pcb").write_text(KICAD9_LAYER_TABLE, encoding="utf-8")
    assert api.copper_layers() == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert api.pads(net="GND")[0]["layers"] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert len(api.notes) == 1 and "needs 9.0.5" in api.notes[0] and "layer table" in api.notes[0]
    slow = api_over(two_pad_board(enabled_layers=TimeoutError("60 s")), tmp_path)
    with pytest.raises(BoardApiError, match="reading enabled layers failed: 60 s"):
        slow.copper_layers()                                 # a transient failure is no fallback
    assert papi.file_copper_layers(tmp_path / "missing.kicad_pcb") == []
    flat = tmp_path / "flat.kicad_pcb"
    flat.write_text('(kicad_pcb (layers (2 "B.Cu" signal) (0 "F.Cu" signal)))', encoding="utf-8")
    assert papi.file_copper_layers(flat) == ["F.Cu", "B.Cu"]


@requires_kipy
def test_pad_refs_fall_back_to_the_footprint_definitions_before_kicad_10_0_6(tmp_path):
    pads = [pad("pA1", None, "1", "OUT_A", 10, 10), pad("pA2", None, "1", "OUT_A", 14, 10),
            pad("pX", None, "9", "OUT_A", 1, 1)]
    footprints = [footprint("fpU1", "U1", "LM358", 10, 10, pad_ids=("pA1",)),
                  footprint("fpC1", "C1", "100n", 14, 11, pad_ids=("pA2",))]
    board = FakeBoard(pads=pads, footprints=footprints, nets=("OUT_A",))
    api = api_over(board, tmp_path, mode="gui", version="10.0.5")
    assert [p["ref"] for p in api.pads()] == ["U1", "C1", ""]
    assert [p["id"] for p in api.pads(ref="C1")] == ["pA2"]
    assert {fp["ref"]: fp["pads"] for fp in api.footprints()} == {"U1": 1, "C1": 1}
    assert len(api.notes) == 1 and "1 pad(s)" in api.notes[0] and "needs 10.0.6" in api.notes[0]


@requires_kipy
def test_resolve_mode_probes_the_gui_only_for_auto(tmp_path, monkeypatch):
    path = tmp_path / "a.kicad_pcb"
    assert BoardApi.resolve_mode(path, "headless") == "headless"
    assert BoardApi.resolve_mode(path, "gui") == "gui"
    with pytest.raises(BoardApiError, match="mode must be one of"):
        BoardApi.resolve_mode(path, "remote")
    closed: list[bool] = []
    probe = SimpleNamespace(open_boards=lambda: [sess.norm_path(path)],
                            close=lambda: closed.append(True))
    monkeypatch.setattr(sess.BoardSession, "gui", classmethod(lambda cls, **_kw: probe))
    assert BoardApi.resolve_mode(path) == "gui" and closed == [True]   # probe closed again
    monkeypatch.setattr(sess.BoardSession, "gui", classmethod(lambda cls, **_kw: None))
    assert BoardApi.resolve_mode(path) == "headless"


@requires_kipy
def test_add_tracks_builds_kipy_tracks(tmp_path):
    board = two_pad_board()
    api = api_over(board, tmp_path)
    assert api.is_modified is False
    made = api.add_tracks("OUT_A", [{"layer": "F.Cu", "start": [10, 10], "end": [12, 10],
                                     "width": 0.35},
                                    {"layer": "B.Cu", "start": (12, 10), "end": (14, 10),
                                     "width": "0.25"}])
    assert api.is_modified is True and board.calls[-1] == "create_items"
    first, second = board.created
    assert isinstance(first, kb.Track) and first.net.name == "OUT_A"
    assert (first.start.x, first.start.y, first.end.x) == (10_000_000, 10_000_000, 12_000_000)
    assert first.width == 350000 and first.layer == kb.BoardLayer.BL_F_Cu
    assert second.layer == kb.BoardLayer.BL_B_Cu and second.width == 250000
    assert [(m["layer"], m["length"], m["width"]) for m in made] == \
        [("F.Cu", 2.0, 0.35), ("B.Cu", 2.0, 0.25)]
    with pytest.raises(BoardApiError, match="at least one segment"):
        api.add_tracks("OUT_A", [])


@requires_kipy
def test_add_vias_builds_kipy_vias(tmp_path):
    board = two_pad_board()
    api = api_over(board, tmp_path)
    made = api.add_vias("GND", [{"x": 14, "y": 12, "diameter": 0.6, "drill": 0.3}])
    assert made == [{"id": "", "net": "GND", "x": 14.0, "y": 12.0, "diameter": 0.6, "drill": 0.3}]
    created = board.created[0]
    assert isinstance(created, kb.Via) and created.net.name == "GND"
    assert (created.position.x, created.diameter, created.drill_diameter) == \
        (14_000_000, 600000, 300000)
    assert api.is_modified is True
    with pytest.raises(BoardApiError, match="drill 0.6 mm must be smaller"):
        api.add_vias("GND", [{"x": 0, "y": 0, "diameter": 0.6, "drill": 0.6}])
    with pytest.raises(BoardApiError, match="lacks \\['drill'\\]"):
        api.add_vias("GND", [{"x": 0, "y": 0, "diameter": 0.6}])
    with pytest.raises(BoardApiError, match="at least one via"):
        api.add_vias("GND", [])


@requires_kipy
def test_unknown_net_and_layer_name_the_valid_ones(tmp_path):
    api = api_over(two_pad_board(), tmp_path)
    with pytest.raises(BoardApiError, match="no net 'VCC'.*nets: \\['GND', 'OUT_A'\\]"):
        api.add_tracks("VCC", [{"layer": "F.Cu", "start": [0, 0], "end": [1, 0], "width": 0.2}])
    with pytest.raises(BoardApiError, match="unknown copper layer 'In3.Cu'.*"
                                            "\\['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'\\]"):
        api.add_tracks("OUT_A", [{"layer": "In3.Cu", "start": [0, 0], "end": [1, 0], "width": 0.2}])
    with pytest.raises(BoardApiError, match="net name must be a non-empty string"):
        api.add_vias("", [{"x": 0, "y": 0, "diameter": 0.6, "drill": 0.3}])
    assert api.is_modified is False


@requires_kipy
@pytest.mark.parametrize("segment,match", [
    ({"layer": "F.Cu", "start": [1, 1], "end": [1, 1], "width": 0.2}, "zero length"),
    ({"layer": "F.Cu", "start": [1, 1], "end": [2, 1]}, "lacks \\['width'\\]"),
    ({"layer": "F.Cu", "start": [1, 1], "end": [2, 1], "width": 0}, "track width must be > 0"),
    ({"layer": "F.Cu", "start": [1, 1], "end": [2, 1], "width": "wide"}, "must be a number"),
    ({"layer": "F.Cu", "start": [1], "end": [2, 1], "width": 0.2}, "start must be \\[x, y\\]"),
    ({"layer": "F.Cu", "start": [1, "y"], "end": [2, 1], "width": 0.2}, "start y must be a number"),
    ("F.Cu 1 1 2 1", "must be a dict"),
])
def test_bad_segments_are_refused_before_kicad_sees_them(tmp_path, segment, match):
    board = two_pad_board()
    with pytest.raises(BoardApiError, match=match):
        api_over(board, tmp_path).add_tracks("OUT_A", [segment])
    assert "create_items" not in board.calls


@requires_kipy
def test_remove_and_remove_net_routing(tmp_path):
    tracks = [track("t1", "OUT_A", "F.Cu", 10, 10, 12, 10), track("t2", "OUT_A", "F.Cu", 12, 10, 14, 10),
              track("t3", "GND", "B.Cu", 0, 0, 1, 1)]
    vias = [via("v1", "OUT_A", 12, 10), via("v2", "GND", 5, 5)]
    board = two_pad_board(tracks=tracks, vias=vias)
    api = api_over(board, tmp_path)
    assert api.remove(["t1", "v2"]) == 2 and api.is_modified is True
    assert [t["id"] for t in api.tracks()] == ["t2", "t3"] and [v["id"] for v in api.vias()] == ["v1"]
    with pytest.raises(BoardApiError, match="not a track or via on this board: \\['pA1', 'ghost'\\]"):
        api.remove(["t2", "pA1", "ghost"])
    assert len(api.tracks()) == 2                        # nothing removed on a refused call
    with pytest.raises(BoardApiError, match="at least one"):
        api.remove([])
    assert api.remove_net_routing("OUT_A") == {"tracks": 1, "vias": 1}
    assert api.tracks(net="OUT_A") == [] and api.tracks(net="GND")[0]["id"] == "t3"
    assert api.remove_net_routing("OUT_A") == {"tracks": 0, "vias": 0}


@requires_kipy
def test_dirty_flag_save_and_refill(tmp_path):
    board = two_pad_board(zones=[zone("z1", "", "GND", ["In1.Cu"], (0, 0, 30, 25))])
    api = api_over(board, tmp_path)
    assert api.refill_zones() == 1 and api.is_modified is True
    assert api.save() == str(tmp_path / "b.kicad_pcb") and api.is_modified is False
    api.add_tracks("OUT_A", [{"layer": "F.Cu", "start": [10, 10], "end": [14, 10], "width": 0.2}])
    copy = api.save_as(tmp_path / "copy.kicad_pcb")
    assert copy == str(tmp_path / "copy.kicad_pcb") and api.is_modified is True
    assert board.calls[-1] == "save_as:True"
    (tmp_path / "taken.kicad_pcb").write_text("x", encoding="utf-8")
    with pytest.raises(BoardApiError, match="overwrite=True"):
        api.save_as(tmp_path / "taken.kicad_pcb", overwrite=False)
    api.close()
    assert api.session.closed
    with pytest.raises(BoardApiError, match="is closed"):
        api.pads()
    api.close()                                          # idempotent


@requires_kipy
def test_discard_reverts_and_closes(tmp_path):
    board = two_pad_board()
    api = api_over(board, tmp_path)
    api.add_vias("GND", [{"x": 1, "y": 1, "diameter": 0.6, "drill": 0.3}])
    api.discard()
    assert board.calls.count("revert") == 1 and api.session.closed
    with pytest.raises(BoardApiError, match="is closed"):
        api.clusters("GND")
    clean = api_over(two_pad_board(), tmp_path)
    clean.discard()                                      # nothing to revert: no RevertDocument
    assert "revert" not in clean.session._board.calls and clean.session.closed
    gui_board = two_pad_board()
    gui = api_over(gui_board, tmp_path, mode="gui")
    gui.add_vias("GND", [{"x": 1, "y": 1, "diameter": 0.6, "drill": 0.3}])
    gui.discard()                                        # the GUI's document is left alone
    assert "revert" not in gui_board.calls and gui.session.closed


@requires_kipy
def test_netclasses_fall_back_to_project_then_rules(tmp_path):
    classes = {"OUT_A": netclass("Signal", track_width=None, via_diameter=None, via_drill=None),
               "": netclass("Default")}
    board = two_pad_board(classes=classes)
    (tmp_path / "b.kicad_pro").write_text(json.dumps({"net_settings": {"classes": [
        {"name": "Signal", "track_width": 0.3, "clearance": 0.25},
        {"name": "Default", "track_width": 0.2, "clearance": 0.2, "via_diameter": 0.6,
         "via_drill": 0.3}]}}), encoding="utf-8")
    got = api_over(board, tmp_path).netclasses()
    assert list(got) == ["GND", "OUT_A"]
    assert got["OUT_A"] == {"name": "Signal", "clearance": 0.2,      # KiCad's own value wins
                            "track_width": 0.3,                      # project class
                            "via_diameter": 0.45, "via_drill": 0.2}  # board minimums
    assert got["GND"] == {"name": "Default", "clearance": 0.2, "track_width": 0.2,
                          "via_diameter": 0.6, "via_drill": 0.3}     # not in KiCad's answer
    assert papi.project_netclasses(tmp_path / "missing.kicad_pcb") == {}
    (tmp_path / "junk.kicad_pro").write_text("{not json", encoding="utf-8")
    assert papi.project_netclasses(tmp_path / "junk.kicad_pcb") == {}


@requires_kipy
def test_rules_unavailable_degrades_to_none_and_a_note(tmp_path):
    board = two_pad_board(rules=kb.ApiError("GetBoardDesignRules not supported"))
    api = api_over(board, tmp_path, mode="gui", version="9.0.4")
    assert api.rules() == items.empty_rules()
    assert api.notes and "design rules unavailable" in api.notes[0]
    assert api.netclasses()["OUT_A"]["track_width"] is None   # nothing to fall back to
    assert api.overview()["notes"] == api.notes


@requires_kipy
def test_overview_shape_and_planes(tmp_path):
    zones = [zone("z1", "gnd", "GND", ["In1.Cu"], (0, 0, 30, 25)),
             zone("z2", "top", "GND", ["F.Cu"], (0, 0, 30, 25)),
             zone("z3", "keep", "", ["In2.Cu"], (1, 1, 2, 2), rule_area=True)]
    shapes = [edge(0, 0, 30, 0), edge(30, 0, 30, 25), edge(30, 25, 0, 25), edge(0, 25, 0, 0)]
    board = two_pad_board(zones=zones, shapes=shapes, tracks=[track("t1", "GND", "F.Cu", 0, 0, 1, 0)],
                          bboxes={"fpU1": box(9, 9, 11, 11)})
    api = api_over(board, tmp_path)
    view = api.overview()
    assert view["copper_layers"] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert view["outline"]["width"] == 30.0 and view["outline"]["segments"] == 4
    assert [z["id"] for z in view["planes"]] == ["z1"]
    assert view["counts"] == {"pads": 3, "tracks": 1, "vias": 0, "zones": 3, "nets": 2}
    assert view["rules"]["min_track_width"] == 0.15
    assert {fp["ref"]: (fp["pads"], fp["bbox"]) for fp in view["footprints"]} == \
        {"U1": (1, [9.0, 9.0, 11.0, 11.0]), "C1": (2, None)}
    assert (view["mode"], view["kicad_version"], view["path"]) == \
        ("headless", "10.99.0-4025-gf7ca62", str(tmp_path / "b.kicad_pcb"))
    assert "notes" not in view
    assert [p["ref"] for p in api.pads(ref="C1")] == ["C1", "C1"]
    assert [p["pad"] for p in api.pads(net="GND", ref="C1")] == ["2"]
    assert api.nets() == ["GND", "OUT_A"]


@requires_kipy
def test_open_refuses_missing_file_and_bad_suffix(tmp_path):
    with pytest.raises(BoardApiError, match="must end with"):
        BoardApi.open(tmp_path / "x.txt")
    with pytest.raises(BoardApiError, match="not found"):
        BoardApi.open(tmp_path / "missing.kicad_pcb")


# ============================================================================
# live tier — headless KiCad 11 on a board built by KiCad 9's pcbnew
# ============================================================================
def _kicad_cli_pids() -> set[int] | None:
    """kicad-cli.exe PIDs this test run owns (Windows; other agents' servers on
    the same machine are not ours); None off Windows."""
    return owned_pids("kicad-cli.exe")


def _assert_no_server_left(before: set[int] | None) -> None:
    if before is not None:
        assert _kicad_cli_pids() <= before, "kicad-cli api-server leaked"


def _format_version(pcb: Path) -> int:
    found = re.search(r"\(version\s+(\d+)\s*\)", pcb.read_text(encoding="utf-8")[:400])
    assert found, "no (version N) header"
    return int(found.group(1))


@pytest.fixture(scope="module")
def unrouted_pcb(tmp_path_factory) -> Path:
    """The lm358 example placed by KiCad 9's pcbnew (planes poured, nothing
    routed) — the input the routing tools will see."""
    if not BoardApi.available():
        pytest.skip(BoardApi.unavailable_reason() or "board API unavailable")
    if not tc.kicad_python():
        pytest.skip(f"no KiCad pcbnew Python to build the board: {tc.kicad_python_error()}")
    out = tmp_path_factory.mktemp("board") / "lm358_buffer.kicad_pcb"
    built = board_pipeline_tools.build_board_from_netlist(
        str(EXAMPLES / "lm358_buffer" / "netlist.json"), 30, 25, out_pcb=str(out))
    assert built.get("ok"), built
    assert _format_version(out) == KICAD9_FORMAT
    return out


def _signal_net(api: BoardApi) -> str:
    """A net with at least two pads that no plane serves."""
    planes = {zone["net"] for zone in api.overview()["planes"]}
    for name in api.nets():
        if name not in planes and len(api.pads(net=name)) >= 2:
            return name
    pytest.fail(f"no routable signal net among {api.nets()}")


@requires_api
def test_live_overview_pads_classes_and_clusters(unrouted_pcb):
    before = _kicad_cli_pids()
    with BoardApi.open(unrouted_pcb, mode="headless") as api:
        assert api.mode == "headless" and not api.is_modified
        view = api.overview()
        assert view["copper_layers"] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
        outline = view["outline"]                # the builder fits the outline to the parts
        assert outline["segments"] >= 4 and len(outline["bbox"]) == 4
        assert outline["width"] > 5 and outline["height"] > 5
        assert outline["width"] == round(outline["bbox"][2] - outline["bbox"][0], 4)
        assert view["counts"]["tracks"] == 0 and view["counts"]["vias"] == 0
        assert {(z["net"], z["layers"][0]) for z in view["planes"]} >= {("GND", "In1.Cu")}
        assert view["rules"]["min_track_width"] > 0 and view["rules"]["min_clearance"] > 0
        assert set(view["netclasses"]) == set(api.nets())
        assert all(cls["track_width"] > 0 and cls["clearance"] > 0
                   for cls in view["netclasses"].values())
        assert "notes" not in view
        pads = api.pads()
        assert pads and all(p["ref"] and p["layers"] and p["net"] is not None for p in pads)
        pth = [p for p in pads if p["through_hole"]]
        assert pth and pth[0]["layers"] == view["copper_layers"] and pth[0]["drill"] > 0
        assert all(len(p["layers"]) == 1 for p in pads if not p["through_hole"])
        assert sum(fp["pads"] for fp in view["footprints"]) == len(pads)
        signal = _signal_net(api)
        assert api.connectivity_available is True
        assert len(api.clusters(signal)) == len(api.pads(net=signal))   # nothing routed
        gnd_pads = api.pads(net="GND")
        gnd_clusters = api.clusters("GND")
        assert sorted(sum(gnd_clusters, [])) == sorted(p["id"] for p in gnd_pads)  # a partition
        # J1.2 is the only GND pad that reaches the In1.Cu plane (the others are
        # top-side SMD pads without a via yet): every pad is still its own cluster
        assert len(gnd_clusters) == len(gnd_pads) == 4
    assert api.session.closed
    _assert_no_server_left(before)


@requires_api
def test_live_route_refill_save_reopen_and_discard(unrouted_pcb):
    before = _kicad_cli_pids()
    api = BoardApi.open(unrouted_pcb, mode="headless")
    try:
        signal = _signal_net(api)                                   # IN_A: J1.3 (PTH) + U1.3
        a, b = api.pads(net=signal)[:2]
        layer = next(name for name in a["layers"] if name in b["layers"])
        assert len(api.clusters(signal)) == 2
        tracks = api.add_tracks(signal, [{"layer": layer, "start": [a["x"], a["y"]],
                                          "end": [b["x"], b["y"]], "width": 0.25}])
        assert len(tracks) == 1 and tracks[0]["id"] and tracks[0]["net"] == signal
        assert abs(tracks[0]["length"] - math.dist((a["x"], a["y"]), (b["x"], b["y"]))) < 1e-3
        assert api.clusters(signal) == [sorted([a["id"], b["id"]])]   # KiCad sees the track
        smd_gnd = next(p for p in api.pads(net="GND") if not p["through_hole"])
        vias = api.add_vias("GND", [{"x": smd_gnd["x"], "y": smd_gnd["y"],
                                     "diameter": 0.6, "drill": 0.3}])
        assert len(vias) == 1 and vias[0]["id"] and vias[0]["net"] == "GND"
        assert api.is_modified
        assert api.refill_zones() >= 1
        assert len(api.clusters("GND")) == 3      # the via reaches the In1.Cu plane J1.2 touches
        assert api.save() == str(unrouted_pcb) and not api.is_modified
    finally:
        api.close()
    assert api.session.closed
    _assert_no_server_left(before)
    assert _format_version(unrouted_pcb) > KICAD9_FORMAT          # now a KiCad 11 file
    with BoardApi.open(unrouted_pcb, mode="headless") as again:
        routed = again.tracks(net=signal)
        assert len(routed) == 1 and routed[0]["width"] == 0.25 and routed[0]["layer"] == layer
        assert len(again.vias(net="GND")) == 1 and again.vias(net=signal) == []
        assert again.remove_net_routing(signal) == {"tracks": 1, "vias": 0}
        assert again.remove_net_routing("GND") == {"tracks": 0, "vias": 1}
        assert again.tracks(net=signal) == [] and again.is_modified
        again.discard()                                             # revert, not save
    _assert_no_server_left(before)
    with BoardApi.open(unrouted_pcb, mode="headless") as third:
        assert len(third.tracks(net=signal)) == 1                   # the file kept the track
    _assert_no_server_left(before)


@requires_api
def test_live_auto_without_a_gui_goes_headless(unrouted_pcb, monkeypatch):
    """No KiCad GUI on the socket -> 'auto' must not fail, it goes headless;
    'gui' says what to do."""
    monkeypatch.setenv("KICAD_API_SOCKET", "ipc://" + str(unrouted_pcb.parent / "nobody.sock"))
    before = _kicad_cli_pids()
    assert sess.BoardSession.gui() is None
    with pytest.raises(BoardApiError, match="no KiCad GUI answers"):
        BoardApi.open(unrouted_pcb, mode="gui")
    with BoardApi.open(unrouted_pcb, mode="auto") as api:
        assert api.mode == "headless"
        assert api.overview()["counts"]["pads"] > 0
    assert api.session.closed
    _assert_no_server_left(before)
