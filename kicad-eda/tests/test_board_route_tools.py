"""board_route_tools — the agent-routing MCP tools over a fake BoardApi.

No KiCad needed: BoardApi is replaced by FakeBoardApi (an in-memory board
in the dict shapes backends.pcb_items produces), kicad-cli by a fake
toolchain.run that writes a DRC report, and the install list by a fake KiCad
11. Covers the registry (open on demand, one headless board at a time,
release / refuse, close errors), every tool's error path (ok false + reason,
never an exception), route_net through validate_path, route_check's report
parsing, route_status's width / ampacity verdicts, routed_gate, the
check_design_gates dispatch for a KiCad-11-format board, the server's
registration + status keys, the pre-KiCad-11 snapshot hook (first KiCad 11
save, KiCad 11 GUI open) and route_check's stall detection (ROADMAP D5). The
fake's save() rewrites an existing board file with a KiCad 11 header, as the
real headless save does.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import math
from pathlib import Path

import pytest

import board_pipeline_tools as bpt
import board_route_tools as brt
import ipc_rules
import kicad_env as env
import kicad_ipc as ipc
import session_release
import toolchain as tc
from backends.errors import BoardApiError

KICAD11_HEADER = '(kicad_pcb (version 20260901) (generator "pcbnew") (generator_version "10.99")\n'
KICAD9_HEADER = '(kicad_pcb (version 20241229) (generator "pcbnew") (generator_version "9.0")\n'
NIGHTLY = "10.99.0-4025-gf7ca62"
COPPER = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
DEFAULT = {"name": "Default", "clearance": 0.2, "track_width": 0.25,
           "via_diameter": 0.6, "via_drill": 0.3}
POWER = {"name": "PWR_3A", "clearance": 0.25, "track_width": 0.5,
         "via_diameter": 0.8, "via_drill": 0.4}
CLASSES = {"SIG": DEFAULT, "OTHER": DEFAULT, "GND": DEFAULT, "PWR": POWER}
RULES = {"min_clearance": 0.15, "min_track_width": 0.15, "min_via_diameter": 0.45,
         "min_via_drill": 0.2, "min_via_annular": 0.1, "copper_edge_clearance": 0.3,
         "hole_clearance": 0.25, "hole_to_hole": 0.25}
NETS = ("GND", "OTHER", "PWR", "SIG")


def _pad(pid: str, ref: str, num: str, net: str, x: float, y: float, *,
         layers: tuple[str, ...] = ("F.Cu",), w: float = 1.0, h: float = 0.6,
         drill: float | None = None) -> dict:
    return {"id": pid, "ref": ref, "pad": num, "net": net, "x": x, "y": y, "layers": list(layers),
            "through_hole": drill is not None, "shape": "roundrect", "w": w, "h": h,
            "rotation": 0.0, "drill": drill}


PADS = [_pad("s1", "U1", "1", "SIG", 10.0, 10.0), _pad("s2", "C1", "1", "SIG", 20.0, 10.0),
        _pad("x1", "R1", "1", "OTHER", 15.0, 10.0),            # sits on the s1-s2 line
        _pad("p1", "U1", "2", "PWR", 10.0, 20.0),
        _pad("p2", "J1", "1", "PWR", 30.0, 20.0, layers=tuple(COPPER), w=1.7, h=1.7, drill=1.0),
        _pad("g1", "C1", "2", "GND", 20.0, 12.0)]
SIG_PATH = [{"layer": "F.Cu", "points": [[10, 10], [14, 10]]}, {"via": [14, 10]},
            {"layer": "B.Cu", "points": [[14, 10], [20, 10]]}]
PWR_PATH = [{"layer": "F.Cu", "points": [[10, 20], [30, 20]]}]
CLEARANCE = {"type": "clearance", "severity": "error",
             "description": "Clearance violation (netclass 'Default' clearance 0.2000 mm; "
                            "actual 0.1000 mm)",
             "items": [{"description": "Track [SIG] on F.Cu", "pos": {"x": 12.5, "y": 10.0},
                        "uuid": "aa"},
                       {"description": "Pad 1 [OTHER] of R1 on F.Cu", "pos": {"x": 15.0, "y": 10.0},
                        "uuid": "bb"}]}
UNCONNECTED = {"type": "unconnected_items", "severity": "error",
               "description": "Missing connection between items",
               "items": [{"description": "Pad 1 [P5V] of C1 on F.Cu",
                          "pos": {"x": 143.525, "y": 92.303}, "uuid": "cc"},
                         {"description": "PTH pad 1 [P5V] of J1",
                          "pos": {"x": 149.429, "y": 93.253}, "uuid": "dd"}]}


# ============================================================================
# the fake backend
# ============================================================================
class FakeBoardApi:
    """In-memory stand-in for backends.pcb_api.BoardApi: records calls,
    numbers created items and answers in the pcb_items dict shapes."""
    opened: list["FakeBoardApi"] = []
    unavailable: str | None = None
    version = NIGHTLY                           # the KiCad every new session reports

    def __init__(self, path: str, mode: str) -> None:
        self.path, self.mode = path, mode
        self.kicad_version = FakeBoardApi.version
        self.snapshot_at_save: list[bool] = []  # did X.k9.kicad_pcb exist at each save?
        self.connectivity_available = True
        self.refuse_connectivity = False        # KiCad says no although the version said yes
        self.is_modified = False
        self.closed = False
        self.calls: list[tuple] = []
        self.notes: list[str] = []
        self.cluster_map: dict[str, list[list[str]]] = {}
        self._pads = copy.deepcopy(PADS)
        self._tracks: list[dict] = []
        self._vias: list[dict] = []
        self._serial = 0
        FakeBoardApi.opened.append(self)

    @classmethod
    def unavailable_reason(cls) -> str | None:
        return cls.unavailable

    @classmethod
    def available(cls) -> bool:
        return cls.unavailable is None

    @classmethod
    def resolve_mode(cls, path, mode: str = "auto") -> str:
        """A board whose FILE name contains 'gui' is open in the (fake)
        running GUI (the directory name does not count: tmp_path carries the
        test's name)."""
        if mode not in ("auto", "gui", "headless"):
            raise BoardApiError(f"mode must be one of ('auto', 'gui', 'headless'), got {mode!r}")
        if mode == "auto":
            return "gui" if "gui" in Path(str(path)).name else "headless"
        return mode

    @classmethod
    def open(cls, path, mode: str = "auto") -> "FakeBoardApi":
        if "boom" in str(path):
            raise BoardApiError("could not start `kicad-cli api-server`: boom")
        return cls(str(path), cls.resolve_mode(path, mode))

    def close(self) -> None:
        self.closed = True

    def discard(self) -> None:
        self.calls.append(("discard",))
        self.closed = True

    # --- reads --------------------------------------------------------------
    def copper_layers(self) -> list[str]:
        return list(COPPER)

    def nets(self) -> list[str]:
        return sorted(NETS)

    def pads(self, net: str = "", ref: str = "") -> list[dict]:
        return [dict(p) for p in self._pads
                if (not net or p["net"] == net) and (not ref or p["ref"] == ref)]

    def tracks(self, net: str = "") -> list[dict]:
        return [dict(t) for t in self._tracks if not net or t["net"] == net]

    def vias(self, net: str = "") -> list[dict]:
        return [dict(v) for v in self._vias if not net or v["net"] == net]

    def rules(self) -> dict:
        return dict(RULES)

    def netclasses(self) -> dict[str, dict]:
        return {net: dict(cls) for net, cls in CLASSES.items()}

    def overview(self) -> dict:
        return {"path": self.path, "mode": self.mode, "kicad_version": self.kicad_version,
                "copper_layers": self.copper_layers(),
                "outline": {"bbox": [0.0, 0.0, 40.0, 30.0], "width": 40.0, "height": 30.0,
                            "segments": 4},
                "rules": self.rules(), "netclasses": self.netclasses(), "planes": [],
                "footprints": [],
                "counts": {"pads": len(self._pads), "tracks": len(self._tracks),
                           "vias": len(self._vias), "zones": 0, "nets": len(NETS)}}

    def clusters(self, net: str) -> list[list[str]]:
        self._check_net(net)
        if self.refuse_connectivity:
            self.connectivity_available = False
        if not self.connectivity_available:
            raise BoardApiError("connectivity unavailable on KiCad 9.0.4 (unsupported); "
                                "GetConnectedItems needs KiCad >= 10.0.1")
        listed = self.cluster_map.get(net)
        if listed is not None:
            return [list(group) for group in listed]
        return [[p["id"]] for p in self._pads if p["net"] == net]

    # --- writes -------------------------------------------------------------
    def _check_net(self, net: str) -> None:
        if net not in NETS:
            raise BoardApiError(f"no net {net!r} on {self.path}; nets: {self.nets()}")

    def _new(self, prefix: str) -> str:
        self._serial += 1
        return f"{prefix}{self._serial}"

    def add_tracks(self, net: str, segments) -> list[dict]:
        self._check_net(net)
        segments = [dict(s) for s in segments]
        self.calls.append(("add_tracks", net, segments))
        made = [{"id": self._new("t"), "net": net, "layer": s["layer"], "start": s["start"],
                 "end": s["end"], "width": s["width"],
                 "length": round(math.dist(s["start"], s["end"]), 4)} for s in segments]
        self._tracks.extend(made)
        self.is_modified = True
        return [dict(t) for t in made]

    def add_vias(self, net: str, vias) -> list[dict]:
        self._check_net(net)
        vias = [dict(v) for v in vias]
        self.calls.append(("add_vias", net, vias))
        made = [{"id": self._new("v"), "net": net, "x": v["x"], "y": v["y"],
                 "diameter": v["diameter"], "drill": v["drill"]} for v in vias]
        self._vias.extend(made)
        self.is_modified = True
        return [dict(v) for v in made]

    def remove(self, ids) -> int:
        wanted = [str(i) for i in ids]
        known = {t["id"] for t in self._tracks} | {v["id"] for v in self._vias}
        unknown = [i for i in wanted if i not in known]
        if unknown:
            raise BoardApiError(f"not a track or via on this board: {unknown}")
        self._tracks = [t for t in self._tracks if t["id"] not in wanted]
        self._vias = [v for v in self._vias if v["id"] not in wanted]
        self.is_modified = True
        return len(wanted)

    def remove_net_routing(self, net: str) -> dict:
        self._check_net(net)
        out = {"tracks": sum(1 for t in self._tracks if t["net"] == net),
               "vias": sum(1 for v in self._vias if v["net"] == net)}
        self._tracks = [t for t in self._tracks if t["net"] != net]
        self._vias = [v for v in self._vias if v["net"] != net]
        if out["tracks"] or out["vias"]:
            self.is_modified = True
        return out

    def refill_zones(self) -> int:
        self.calls.append(("refill_zones",))
        self.is_modified = True
        return 2

    def save(self) -> str:
        """Like a KiCad 11 save: an existing board file is rewritten in KiCad 11 format."""
        self.calls.append(("save",))
        self.snapshot_at_save.append(Path(tc.pre_k11_snapshot_path(self.path)).is_file())
        if Path(self.path).is_file() and env.parse_version(self.kicad_version) >= (10, 99, 0):
            Path(self.path).write_text(KICAD11_HEADER, encoding="utf-8")
        self.is_modified = False
        return self.path


@pytest.fixture(autouse=True)
def fake_api(monkeypatch):
    """The board tools run over FakeBoardApi with an empty registry."""
    FakeBoardApi.opened = []
    FakeBoardApi.unavailable = None
    FakeBoardApi.version = NIGHTLY
    monkeypatch.setattr(brt, "BoardApi", FakeBoardApi)
    monkeypatch.setattr(brt, "_BOARDS", {})
    monkeypatch.setattr(brt, "_PROGRESS", {})
    return FakeBoardApi


@pytest.fixture
def pcb(tmp_path) -> str:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(KICAD11_HEADER, encoding="utf-8")
    return str(path)


@pytest.fixture
def kicad11(monkeypatch, tmp_path) -> env.KiCadInstall:
    """The only install: a 10.99 nightly (kicad-cli 11 reads the KiCad 11 file)."""
    root = tmp_path / "10.99"
    inst = env.KiCadInstall(version="10.99", version_tuple=(10, 99, 0), root=root,
                            kicad_cli=root / "bin" / "kicad-cli.exe",
                            share=root / "share" / "kicad", has_api_server=True)
    monkeypatch.setattr(env, "discover", lambda refresh=False: [inst])
    return inst


def _done():
    return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()


def _drc_writer(monkeypatch, report: dict) -> list[list[str]]:
    """tc.run that records argv and writes `report` where kicad-cli would."""
    calls: list[list[str]] = []

    def fake_run(args, **kw):
        calls.append(list(args))
        Path(args[args.index("-o") + 1]).write_text(json.dumps(report), encoding="utf-8")
        return _done()

    monkeypatch.setattr(tc, "run", fake_run)
    return calls


def _api() -> FakeBoardApi:
    return FakeBoardApi.opened[-1]


# ============================================================================
# tool surface + registry
# ============================================================================
def test_every_tool_has_a_docstring_and_takes_pcb_first():
    names = [fn.__name__ for fn in brt.TOOLS]
    assert len(set(names)) == len(names) == 12
    for fn in brt.TOOLS:
        assert fn.__doc__ and len(fn.__doc__.strip()) > 60, fn.__name__
        params = list(inspect.signature(fn).parameters)
        if fn is brt.list_open_boards:
            assert params == []
        else:
            assert params[0] == "pcb", fn.__name__


def test_tools_open_the_board_on_demand_and_stamp_the_response(pcb, fake_api):
    res = brt.board_pads(pcb)
    assert res["ok"] and res["backend"] == "api" and res["mode"] == "headless"
    assert res["path"] == pcb and res["count"] == len(PADS)
    assert [p["id"] for p in res["pads"]][:2] == ["s1", "s2"]
    assert len(fake_api.opened) == 1
    brt.board_pads(pcb, net="SIG")                          # reuses the session
    assert len(fake_api.opened) == 1
    assert brt.list_open_boards() == {"ok": True, "backend": "api", "boards": [
        {"path": pcb, "mode": "headless", "unsaved_changes": False}]}


def test_open_board_returns_the_overview_and_session_facts(pcb):
    res = brt.open_board(pcb)
    assert res["ok"] and res["opened"] is True and res["mode"] == "headless"
    assert res["kicad_version"].startswith("10.99") and res["connectivity_available"] is True
    assert res["copper_layers"] == COPPER and res["counts"]["pads"] == len(PADS)
    assert res["netclasses"]["PWR"]["track_width"] == 0.5 and res["rules"] == RULES


def test_open_board_reopens_a_saved_board_and_refuses_a_dirty_one(pcb, fake_api):
    brt.open_board(pcb)
    again = brt.open_board(pcb, mode="headless")
    assert again["ok"] and fake_api.opened[0].closed and len(fake_api.opened) == 2
    brt.add_vias(pcb, "GND", [[20, 12]])                    # dirty
    refused = brt.open_board(pcb)
    assert refused["ok"] is False and "unsaved changes" in refused["reason"]
    assert "close_board" in refused["reason"] and refused["mode"] == "headless"
    assert len(fake_api.opened) == 2 and not fake_api.opened[1].closed


def test_open_failures_are_ok_false_with_a_reason(pcb, tmp_path, fake_api):
    boom = brt.open_board(str(tmp_path / "boom.kicad_pcb"))
    assert boom["ok"] is False and "boom" in boom["reason"] and boom["backend"] == "api"
    bad = brt.open_board(pcb, mode="remote")
    assert bad["ok"] is False and "mode must be one of" in bad["reason"]
    assert brt.list_open_boards()["boards"] == [] and fake_api.opened == []


def test_headless_holds_one_board_at_a_time(pcb, tmp_path, fake_api):
    other = str(tmp_path / "other.kicad_pcb")
    brt.route_net(pcb, "SIG", SIG_PATH)                     # a dirty headless board
    refused = brt.board_pads(other)
    assert refused["ok"] is False and "unsaved changes" in refused["reason"]
    assert "save_board or close_board" in refused["reason"]
    assert refused["path"] == other and refused["blocking_board"] == pcb   # who asked, who blocks
    assert refused["mode"] == "headless" and len(fake_api.opened) == 1
    assert brt.save_board(pcb)["saved"] is True
    ok = brt.board_pads(other)
    assert ok["ok"] and ok["released_boards"] == [pcb] and "open_board" in ok["released_note"]
    assert fake_api.opened[0].closed is True
    assert [b["path"] for b in brt.list_open_boards()["boards"]] == [other]


def test_gui_boards_are_never_released_and_never_block(pcb, tmp_path):
    gui = str(tmp_path / "gui.kicad_pcb")
    assert brt.open_board(gui)["mode"] == "gui"
    brt.add_vias(gui, "GND", [[20, 12]])                    # a dirty GUI board
    res = brt.board_pads(pcb)
    assert res["ok"] and "released_boards" not in res
    assert [b["path"] for b in brt.list_open_boards()["boards"]] == [gui, pcb]


def test_auto_serves_a_gui_board_while_another_headless_board_is_dirty(pcb, tmp_path, fake_api):
    """The mode is resolved first: a board the running GUI holds needs no
    headless server, so the dirty headless board neither blocks nor is
    released."""
    gui = str(tmp_path / "gui.kicad_pcb")
    brt.route_net(pcb, "SIG", SIG_PATH)                     # a dirty headless board
    res = brt.board_pads(gui)                               # 'auto' -> the GUI
    assert res["ok"] and res["mode"] == "gui" and "released_boards" not in res
    assert [b["path"] for b in brt.list_open_boards()["boards"]] == [pcb, gui]
    assert len(fake_api.opened) == 2 and not fake_api.opened[0].closed
    assert fake_api.opened[0].is_modified is True
    forced = brt.open_board(str(tmp_path / "other.kicad_pcb"), mode="headless")
    assert forced["ok"] is False and forced["blocking_board"] == pcb


def test_tools_report_an_unexpected_exception_instead_of_raising(pcb):
    brt.open_board(pcb)

    def broken():
        raise AttributeError("'BoardShape' object has no attribute 'bounding_box'")

    _api().overview = broken
    res = brt.board_overview(pcb)
    assert res["ok"] is False and res["reason"].startswith("unexpected AttributeError: ")
    assert "bounding_box" in res["reason"] and res["path"] == pcb and res["mode"] == "headless"
    assert brt.list_open_boards()["boards"][0]["path"] == pcb    # the board stays open


def test_close_all_releases_every_board_for_atexit(pcb, tmp_path, fake_api):
    def broken_close():
        raise BoardApiError("api-server did not stop")

    brt.route_net(pcb, "SIG", SIG_PATH)
    gui = str(tmp_path / "gui.kicad_pcb")
    brt.open_board(gui)
    fake_api.opened[1].close = broken_close
    assert brt.close_all() == ["BoardApiError: api-server did not stop"]
    assert brt.list_open_boards()["boards"] == [] and fake_api.opened[0].closed
    assert brt.close_all() == []


def test_release_close_errors_are_reported_not_dropped(pcb, tmp_path, fake_api):
    def broken_close():
        raise BoardApiError("api-server did not stop")

    brt.open_board(pcb)
    fake_api.opened[0].close = broken_close
    other = str(tmp_path / "other.kicad_pcb")
    res = brt.board_pads(other)
    assert res["ok"] and res["released_boards"] == [pcb]
    assert res["released_board_close_errors"] == [f"{pcb}: BoardApiError: api-server did not stop"]
    fake_api.opened[1].close = broken_close
    again = brt.open_board(other)
    assert again["ok"] and again["previous_session_close_error"] == \
        "BoardApiError: api-server did not stop"
    fake_api.opened[2].close = broken_close
    closed = brt.close_board(other)
    assert closed["ok"] and closed["closed"] is True
    assert closed["close_error"] == "BoardApiError: api-server did not stop"
    assert brt.list_open_boards()["boards"] == []


def test_release_fields_are_keyed_by_kind():
    board = session_release.Release(released=("a",), errors=("a: x",), kind="board").fields()
    assert board["released_boards"] == ["a"] and board["released_board_close_errors"] == ["a: x"]
    assert "open_board" in board["released_note"]
    sch = session_release.Release(released=("s",)).fields()
    assert sch["released_sessions"] == ["s"] and "load_schematic" in sch["released_note"]
    assert session_release.Release().fields() == {}
    with pytest.raises(ValueError, match="kind must be one of"):
        session_release.Release(released=("a",), kind="thing").fields()


def test_status_reports_the_board_api_and_the_open_boards(pcb, fake_api):
    brt.open_board(pcb)
    st = brt.status()
    assert st == {"board_api": True, "board_api_reason": None,
                  "boards_open": [{"path": pcb, "mode": "headless", "unsaved_changes": False}]}
    fake_api.unavailable = "no KiCad with `kicad-cli api-server` found"
    st = brt.status()
    assert st["board_api"] is False and "api-server" in st["board_api_reason"]


def test_server_status_and_registration_include_the_board_tools(pcb, monkeypatch):
    import kicad_server as k
    monkeypatch.setattr(ipc, "connect", lambda: {"ipc_connected": False, "reason": "test: no GUI"})
    brt.open_board(pcb)
    st = k.server_status()
    assert st["board_api"] is True and st["board_api_reason"] is None
    assert st["boards_open"] == [{"path": pcb, "mode": "headless", "unsaved_changes": False}]
    registered = {t.name for t in asyncio.run(k.mcp.list_tools())}
    assert {fn.__name__ for fn in brt.TOOLS} <= registered


# ============================================================================
# reads
# ============================================================================
def test_board_pads_filters_and_refuses_unknown_names(pcb):
    assert [p["id"] for p in brt.board_pads(pcb, net="PWR")["pads"]] == ["p1", "p2"]
    assert [p["id"] for p in brt.board_pads(pcb, ref="C1")["pads"]] == ["s2", "g1"]
    assert brt.board_pads(pcb, net="PWR", ref="J1")["count"] == 1
    bad = brt.board_pads(pcb, net="NOPE")
    assert bad["ok"] is False and "no net 'NOPE'" in bad["reason"] and "'SIG'" in bad["reason"]
    assert "no footprint 'Z9'" in brt.board_pads(pcb, ref="Z9")["reason"]


def test_board_overview_adds_routing_counts_and_unrouted(pcb):
    res = brt.board_overview(pcb)
    assert res["ok"] and res["counts"]["pads"] == len(PADS)
    assert res["routing"] == {"tracks": 0, "vias": 0, "length": 0.0, "nets_with_copper": 0}
    assert res["unrouted"] == 2 and res["connectivity_available"] is True   # SIG and PWR
    brt.route_net(pcb, "SIG", SIG_PATH)
    _api().cluster_map["SIG"] = [["s1", "s2"]]
    res = brt.board_overview(pcb)
    assert res["routing"] == {"tracks": 2, "vias": 1, "length": 10.0, "nets_with_copper": 1}
    assert res["unrouted"] == 1


def test_board_overview_without_connectivity_reports_null_unrouted(pcb):
    brt.open_board(pcb)
    _api().connectivity_available = False
    res = brt.board_overview(pcb)
    assert res["ok"] and res["unrouted"] is None and res["connectivity_available"] is False
    assert "route_check" in res["note"] and "9.0.4" not in res["note"]


def test_unrouted_connections_lists_links_with_obstacles_on_the_outer_layers(pcb):
    res = brt.unrouted_connections(pcb)
    assert res["ok"] and res["unrouted"] == 2 and res["connectivity_available"] is True
    assert [c["net"] for c in res["connections"]] == ["PWR", "SIG"]
    sig = res["connections"][1]
    assert (sig["from"]["id"], sig["to"]["id"], sig["length"]) == ("s1", "s2", 10.0)
    assert set(sig["obstacles"]) == {"F.Cu", "B.Cu"}
    hit = sig["obstacles"]["F.Cu"]
    assert [(o["kind"], o["id"], o["ref"], o["pad"], o["net"]) for o in hit] == \
        [("pad", "x1", "R1", "1", "OTHER")]
    assert hit[0]["distance"] < 0 and hit[0]["t"] == 0.5     # sits on the line, halfway
    assert sig["obstacles"]["B.Cu"] == []                     # x1 has copper on F.Cu only
    assert res["connections"][0]["obstacles"] == {"F.Cu": [], "B.Cu": []}


def test_unrouted_connections_net_filter_clearance_and_flags(pcb):
    res = brt.unrouted_connections(pcb, net="PWR", with_obstacles=False, clearance=0.5)
    assert res["unrouted"] == 1 and [c["net"] for c in res["connections"]] == ["PWR"]
    assert "obstacles" not in res["connections"][0]
    assert brt.unrouted_connections(pcb, net="NOPE")["ok"] is False
    assert "clearance must be >= 0" in brt.unrouted_connections(pcb, clearance=-1)["reason"]
    _api().cluster_map["PWR"] = [["p1", "p2"]]
    assert brt.unrouted_connections(pcb, net="PWR")["unrouted"] == 0


def test_unrouted_connections_without_connectivity(pcb):
    brt.open_board(pcb)
    _api().connectivity_available = False
    res = brt.unrouted_connections(pcb)
    assert res["ok"] and res["unrouted"] is None and res["connections"] == []
    assert res["connectivity_available"] is False and "unconnected_items" in res["note"]
    _api().connectivity_available = True
    _api().refuse_connectivity = True                        # KiCad refuses despite the version
    res = brt.unrouted_connections(pcb)
    assert res["ok"] and res["unrouted"] is None and "connectivity unavailable" in res["note"]


# ============================================================================
# writes
# ============================================================================
def test_route_net_lays_the_validated_path(pcb):
    res = brt.route_net(pcb, "SIG", SIG_PATH)
    assert res["ok"] and res["created"] == {"tracks": 2, "vias": 1}
    assert res["ids"] == ["t1", "t2", "v3"] and res["length"] == 10.0
    assert res["warnings"] == [] and res["net"] == "SIG"
    api = _api()
    assert api.calls == [
        ("add_tracks", "SIG", [
            {"layer": "F.Cu", "start": [10.0, 10.0], "end": [14.0, 10.0], "width": 0.25},
            {"layer": "B.Cu", "start": [14.0, 10.0], "end": [20.0, 10.0], "width": 0.25}]),
        ("add_vias", "SIG", [{"x": 14.0, "y": 10.0, "diameter": 0.6, "drill": 0.3}])]
    assert api.is_modified is True
    assert brt.list_open_boards()["boards"][0]["unsaved_changes"] is True


def test_route_net_takes_widths_and_via_sizes_from_the_class_and_warns(pcb):
    res = brt.route_net(pcb, "PWR", PWR_PATH + [{"via": [30, 20]}])
    assert res["ok"] and _api().calls[0][2][0]["width"] == 0.5
    assert _api().calls[1][2] == [{"x": 30.0, "y": 20.0, "diameter": 0.8, "drill": 0.4}]
    bent = brt.route_net(pcb, "PWR", [{"layer": "F.Cu", "points": [[10, 20], [12, 21], [14, 21]]}])
    assert bent["ok"] and any("not a multiple of 45" in w for w in bent["warnings"])


def test_route_net_validation_errors_become_ok_false_and_add_nothing(pcb):
    missing_via = [{"layer": "F.Cu", "points": [[10, 10], [14, 10]]},
                   {"layer": "B.Cu", "points": [[14, 10], [20, 10]]}]
    res = brt.route_net(pcb, "SIG", missing_via)
    assert res["ok"] is False and "without a via" in res["reason"] and res["backend"] == "api"
    narrow = brt.route_net(pcb, "PWR", PWR_PATH, width=0.3)
    assert narrow["ok"] is False and "PWR_3A class track width 0.5" in narrow["reason"]
    unknown = brt.route_net(pcb, "NOPE", SIG_PATH)
    assert unknown["ok"] is False and "no net 'NOPE'" in unknown["reason"]
    assert "'SIG'" in unknown["reason"]
    assert brt.route_net(pcb, "", SIG_PATH)["ok"] is False
    assert _api().calls == [] and _api().is_modified is False
    assert brt.route_net(pcb, "PWR", PWR_PATH, width=0.3, allow_narrow=True)["ok"] is True


def test_route_net_via_failure_names_the_tracks_already_laid(pcb):
    brt.open_board(pcb)

    def failing(net, vias):
        raise BoardApiError("creating items failed: KiCad refused")

    _api().add_vias = failing
    res = brt.route_net(pcb, "SIG", SIG_PATH)
    assert res["ok"] is False and "KiCad refused" in res["reason"]
    assert "remove_routing(ids=['t1', 't2'])" in res["reason"]
    assert len(_api().tracks()) == 2


def test_add_vias_defaults_from_the_class_and_checks_the_sizes(pcb):
    res = brt.add_vias(pcb, "PWR", [[12, 20], [28, 20]])
    assert res["ok"] and res["created"] == {"tracks": 0, "vias": 2} and res["ids"] == ["v1", "v2"]
    assert _api().calls[0] == ("add_vias", "PWR", [
        {"x": 12.0, "y": 20.0, "diameter": 0.8, "drill": 0.4},
        {"x": 28.0, "y": 20.0, "diameter": 0.8, "drill": 0.4}])
    ring = brt.add_vias(pcb, "SIG", [[15, 15]], diameter=0.5, drill=0.4)
    assert ring["ok"] is False and "annular ring" in ring["reason"]
    inverted = brt.add_vias(pcb, "SIG", [[15, 15]], diameter=0.4, drill=0.5)
    assert inverted["reason"].startswith("drill 0.5 mm >= diameter 0.4 mm")
    small = brt.add_vias(pcb, "SIG", [[15, 15]], diameter=0.4, drill=0.2)
    assert "via diameter 0.4 mm is below the board minimum 0.45 mm" in small["reason"]
    assert "points[1] must be [x, y]" in brt.add_vias(pcb, "SIG", [[1, 2], [3]])["reason"]
    assert "non-empty" in brt.add_vias(pcb, "SIG", [])["reason"]
    assert brt.add_vias(pcb, "NOPE", [[1, 1]])["ok"] is False
    assert len(_api().calls) == 1


def test_remove_routing_requires_net_or_ids_and_counts_by_kind(pcb):
    brt.route_net(pcb, "SIG", SIG_PATH)                     # t1, t2, v3
    brt.add_vias(pcb, "PWR", [[12, 20]])                    # v4
    none = brt.remove_routing(pcb)
    assert none["ok"] is False and "give net" in none["reason"]
    by_ids = brt.remove_routing(pcb, ids=["t1", "v4", "t1"])
    assert by_ids["ok"] and by_ids["removed"] == {"tracks": 1, "vias": 1}
    assert by_ids["ids"] == ["t1", "v4"]
    by_net = brt.remove_routing(pcb, net="SIG")
    assert by_net["removed"] == {"tracks": 1, "vias": 1}
    assert _api().tracks() == [] and _api().vias() == []
    unknown = brt.remove_routing(pcb, ids=["t1"])
    assert unknown["ok"] is False and "not a track or via" in unknown["reason"]
    assert brt.remove_routing(pcb, net="NOPE")["ok"] is False


# ============================================================================
# verify: route_check (kicad-cli DRC) and route_status (the routed gate)
# ============================================================================
def test_route_check_refills_saves_and_parses_the_drc_report(pcb, kicad11, monkeypatch):
    calls = _drc_writer(monkeypatch, {"violations": [CLEARANCE], "unconnected_items": [UNCONNECTED]})
    brt.route_net(pcb, "SIG", SIG_PATH)
    res = brt.route_check(pcb)
    assert res["ok"] is False and res["errors"] == 1 and res["unconnected"] == 1
    assert res["types"] == {"clearance": 1}
    assert res["violations"] == [{
        "type": "clearance", "severity": "error", "description": CLEARANCE["description"],
        "items": [{"description": "Track [SIG] on F.Cu", "x": 12.5, "y": 10.0},
                  {"description": "Pad 1 [OTHER] of R1 on F.Cu", "x": 15.0, "y": 10.0}]}]
    assert res["unconnected_items"][0]["type"] == "unconnected_items"
    assert res["unconnected_items"][0]["items"][1] == \
        {"description": "PTH pad 1 [P5V] of J1", "x": 149.429, "y": 93.253}
    assert res["saved"] is True and res["zones_refilled"] == 2 and "unsaved_note" not in res
    assert res["kicad_cli"] == str(kicad11.kicad_cli) and res["report"].endswith("board_drc.json")
    assert "kicad_cli_note" not in res and res["mode"] == "headless"
    api = _api()
    assert api.calls[-2:] == [("refill_zones",), ("save",)] and api.is_modified is False
    assert calls[0][:4] == [str(kicad11.kicad_cli), "pcb", "drc", pcb]
    assert "--severity-error" in calls[0] and "json" in calls[0]


def test_route_check_is_ok_when_clean_and_truncates_to_max_items(pcb, kicad11, monkeypatch):
    _drc_writer(monkeypatch, {"violations": [], "unconnected_items": []})
    res = brt.route_check(pcb, refill=False)
    assert res["ok"] is True and res["errors"] == 0 and res["unconnected"] == 0
    assert res["violations"] == [] and res["unconnected_items"] == [] and "zones_refilled" not in res
    # an untouched board is never rewritten (a KiCad 9 file would turn into KiCad 11 format)
    assert res["saved"] is False and "nothing to save" in res["save_note"]
    assert ("save",) not in _api().calls and "unsaved_note" not in res
    _drc_writer(monkeypatch, {"violations": [CLEARANCE] * 3, "unconnected_items": [UNCONNECTED] * 2})
    res = brt.route_check(pcb, max_items=1)                 # the refill dirties it: saved
    assert res["saved"] is True and "save_note" not in res and _api().calls[-1] == ("save",)
    assert res["errors"] == 3 and len(res["violations"]) == 1
    assert res["unconnected"] == 2 and len(res["unconnected_items"]) == 1
    assert "max_items must be >= 0" in brt.route_check(pcb, max_items=-1)["reason"]


def test_route_check_without_save_notes_unsaved_changes(pcb, kicad11, monkeypatch):
    _drc_writer(monkeypatch, {"violations": [], "unconnected_items": []})
    brt.route_net(pcb, "SIG", SIG_PATH)
    res = brt.route_check(pcb, refill=False, save=False)
    assert res["ok"] and res["saved"] is False and "unsaved changes" in res["unsaved_note"]
    assert _api().is_modified is True and ("save",) not in _api().calls
    brt.save_board(pcb)
    assert "unsaved_note" not in brt.route_check(pcb, refill=False, save=False)


def test_route_check_without_kicad_cli(pcb, monkeypatch):
    monkeypatch.setattr(env, "discover", lambda refresh=False: [])
    monkeypatch.setattr(tc, "run", lambda args, **kw: pytest.fail("ran kicad-cli without one"))
    res = brt.route_check(pcb)
    assert res["ok"] is False and "kicad-cli not found" in res["reason"]
    assert res["backend"] == "api" and _api().calls[-1] == ("save",)


def test_route_check_reports_a_drc_that_produced_no_report(pcb, kicad11, monkeypatch):
    monkeypatch.setattr(tc, "run", lambda args, **kw: _done())
    res = brt.route_check(pcb, refill=False)
    assert res["ok"] is False and res["reason"].startswith("DRC report unreadable")
    assert res["report"].endswith("_drc.json") and res["saved"] is False and "errors" not in res


def test_route_status_reports_per_net_state_and_the_width_gate(pcb):
    brt.route_net(pcb, "PWR", PWR_PATH, width=0.25, allow_narrow=True)
    brt.route_net(pcb, "SIG", [{"layer": "F.Cu", "points": [[10, 10], [20, 10]]}],
                  width=0.2, allow_narrow=True)
    _api().cluster_map.update({"PWR": [["p1", "p2"]], "SIG": [["s1", "s2"]]})
    res = brt.route_status(pcb)
    assert res["ok"] and res["unrouted"] == 0 and res["complete"] is True
    assert res["nets"]["PWR"] == {
        "pads": 2, "clusters": 1, "complete": True, "tracks": 1, "vias": 0, "length": 20.0,
        "min_width": 0.25, "class": "PWR_3A", "class_width": 0.5, "width_ok": False}
    # a Default-class net carries no width binding (SWIG gate parity): not policed
    assert res["nets"]["SIG"]["width_ok"] is True and res["nets"]["SIG"]["class"] == "Default"
    assert res["nets"]["GND"] == {
        "pads": 1, "clusters": 1, "complete": True, "tracks": 0, "vias": 0, "length": 0.0,
        "min_width": None, "class": "Default", "class_width": 0.25, "width_ok": True}
    assert res["width_issues"] == [
        "PWR: open-field segment routed at 0.250 mm but the net class requires 0.500 mm"]
    assert res["via_ampacity_issues"] == [] and res["passed"] is False
    assert res["connectivity_available"] is True and "notes" not in res
    brt.remove_routing(pcb, net="PWR")
    brt.route_net(pcb, "PWR", PWR_PATH)
    fixed = brt.route_status(pcb)
    assert fixed["width_issues"] == [] and fixed["passed"] is True


def test_route_status_judges_via_ampacity_from_the_netlist_specs(pcb, tmp_path):
    netlist = tmp_path / "netlist.json"
    netlist.write_text(json.dumps({"net_specs": {"PWR": {"voltage": 5.0, "current": 50.0},
                                                 "SIG": {"current": 0.1}}}), encoding="utf-8")
    brt.add_vias(pcb, "PWR", [[20, 20]])
    _api().cluster_map.update({"PWR": [["p1", "p2"]], "SIG": [["s1", "s2"]]})
    res = brt.route_status(pcb, str(netlist))
    cap = ipc_rules.via_ampacity_a(0.4) + ipc_rules.via_ampacity_a(1.0)   # via + J1's PTH barrel
    assert len(res["via_ampacity_issues"]) == 1
    issue = res["via_ampacity_issues"][0]
    assert issue.startswith("PWR: 50.0 A crosses layers through 1 via(s) + 1 PTH barrel(s)")
    assert f"totaling {round(cap, 2)} A" in issue
    assert res["passed"] is False and res["nets"]["PWR"]["vias"] == 1 and res["complete"] is True
    assert brt.route_status(pcb, str(tmp_path / "missing.json"))["ok"] is False
    netlist.write_text("[]", encoding="utf-8")
    assert "JSON object" in brt.route_status(pcb, str(netlist))["reason"]


def test_route_status_judges_a_prerouted_fat_path_by_its_authored_length(pcb, tmp_path):
    """SWIG gate parity: min(1 mm, half the authored fat run) must survive.
    The rpi5 SW1 preroute authors 1.5 mm of 1.375 mm copper, so 0.8 mm is
    enough and 0.7 mm is not."""
    netlist = tmp_path / "netlist.json"
    netlist.write_text(json.dumps({"preroutes": [
        {"net": "PWR", "width": 1.375, "layer": "F.Cu",
         "path": [["pad", "U1", "2", 0.6], ["xy", 44.3, 9.14, 1.375], ["xy", 45.53, 10.0]]}]}),
        encoding="utf-8")
    brt.route_net(pcb, "PWR", [{"layer": "F.Cu", "points": [[10, 20], [10.8, 20]]}], width=1.375)
    _api().cluster_map.update({"PWR": [["p1", "p2"]], "SIG": [["s1", "s2"]]})
    res = brt.route_status(pcb, str(netlist))
    assert res["width_issues"] == [] and res["nets"]["PWR"]["width_ok"] is True
    brt.remove_routing(pcb, net="PWR")
    brt.route_net(pcb, "PWR", [{"layer": "F.Cu", "points": [[10, 20], [10.7, 20]]}], width=1.375)
    short = brt.route_status(pcb, str(netlist))
    assert short["width_issues"] == ["PWR: prerouted 1.375 mm power path missing from the routed "
                                     "board (0.7 mm of fat copper survives, authored 1.5 mm — "
                                     "removed?)"]
    assert short["nets"]["PWR"]["width_ok"] is False and short["passed"] is False


def test_route_status_without_connectivity(pcb):
    brt.open_board(pcb)
    _api().connectivity_available = False
    _api().notes.append("design rules unavailable")
    res = brt.route_status(pcb)
    assert res["ok"] and res["unrouted"] is None and res["complete"] is None
    assert res["passed"] is False and res["connectivity_available"] is False
    assert res["nets"]["SIG"]["clusters"] is None and res["nets"]["SIG"]["complete"] is None
    assert "unconnected_items" in res["note"] and res["notes"] == ["design rules unavailable"]


def test_routed_gate_folds_route_status_into_the_gate_shape(pcb, tmp_path):
    netlist = tmp_path / "netlist.json"
    netlist.write_text(json.dumps({"net_specs": {"SIG": {"diff_pair": "OTHER"}}}), encoding="utf-8")
    brt.route_net(pcb, "PWR", PWR_PATH, width=0.25, allow_narrow=True)
    _api().cluster_map["PWR"] = [["p1", "p2"]]
    res = brt.routed_gate(pcb, str(netlist))
    assert res["ok"] and res["stage"] == "routed" and res["backend"] == "api"
    assert res["passed"] is False and res["unrouted"] == 1 and res["complete"] is False
    assert res["issues"] == [
        "PWR: open-field segment routed at 0.250 mm but the net class requires 0.500 mm",
        "1 connection(s) still unrouted on ['SIG']"]
    assert len(res["warnings"]) == 1 and "diff pair" in res["warnings"][0]
    brt.save_board(pcb)                     # idle now, so the next open is attempted
    failed = brt.routed_gate(str(tmp_path / "boom.kicad_pcb"))
    assert failed["ok"] is False and failed["passed"] is False and failed["stage"] == "routed"
    assert "boom" in failed["reason"] and failed["issues"] == []


def test_check_design_gates_sends_a_kicad11_board_to_the_api_gate(pcb, tmp_path, monkeypatch):
    refusal = ("board.kicad_pcb was saved by KiCad 10.99 (format 20260901); KiCad 9.0.4's "
               "pcbnew Python cannot read it")
    monkeypatch.setattr(tc, "kicad_python", lambda: "S:/9.0/python.exe")
    monkeypatch.setattr(tc, "swig_readable", lambda p: refusal)
    monkeypatch.setattr(tc, "run", lambda args, **kw: pytest.fail("the SWIG gate script ran"))
    netlist = str(tmp_path / "netlist.json")
    seen: list[tuple] = []
    monkeypatch.setattr(brt, "routed_gate", lambda p, n: seen.append((p, n)) or {
        "ok": True, "stage": "routed", "passed": True, "issues": [], "warnings": [],
        "backend": "api"})
    res = bpt.check_design_gates(pcb, netlist, "routed")
    assert res["passed"] is True and res["backend"] == "api" and seen == [(pcb, netlist)]
    placement = bpt.check_design_gates(pcb, netlist, "placement")
    assert placement == {"ok": False, "reason": refusal}


def test_check_design_gates_routed_stage_runs_the_real_api_gate(pcb, monkeypatch):
    monkeypatch.setattr(tc, "kicad_python", lambda: "S:/9.0/python.exe")
    monkeypatch.setattr(tc, "swig_readable", lambda p: "refused")
    monkeypatch.setattr(tc, "run", lambda args, **kw: pytest.fail("the SWIG gate script ran"))
    res = bpt.check_design_gates(pcb, "", "routed")
    assert res["ok"] and res["stage"] == "routed" and res["backend"] == "api"
    assert res["passed"] is False and res["warnings"] == []
    assert res["issues"] == ["2 connection(s) still unrouted on ['PWR', 'SIG']"]


# ============================================================================
# lifecycle
# ============================================================================
def test_save_and_close_board(pcb, fake_api):
    not_open = brt.save_board(pcb)
    assert not_open["ok"] is False and "not open" in not_open["reason"]
    brt.route_net(pcb, "SIG", SIG_PATH)
    saved = brt.save_board(pcb)
    assert saved["ok"] and saved["saved"] is True and saved["path"] == pcb
    assert _api().is_modified is False
    closed = brt.close_board(pcb)
    assert closed == {"ok": True, "backend": "api", "mode": "headless", "path": pcb,
                      "saved": False, "discarded_changes": False, "closed": True}
    assert fake_api.opened[0].closed and brt.list_open_boards()["boards"] == []
    brt.route_net(pcb, "SIG", SIG_PATH)                     # a fresh, dirty session
    with_save = brt.close_board(pcb)
    assert with_save["saved"] is True and with_save["discarded_changes"] is False
    assert ("save",) in fake_api.opened[1].calls
    brt.route_net(pcb, "SIG", SIG_PATH)
    dropped = brt.close_board(pcb, save=False)
    assert dropped["saved"] is False and dropped["discarded_changes"] is True
    assert ("save",) not in fake_api.opened[2].calls and "note" not in dropped
    assert fake_api.opened[2].calls[-1] == ("discard",)     # reverted, then closed
    assert brt.close_board(pcb)["ok"] is False


def test_close_board_without_save_leaves_gui_copper_in_the_editor(tmp_path, fake_api):
    gui = str(tmp_path / "gui.kicad_pcb")
    brt.route_net(gui, "SIG", SIG_PATH)
    res = brt.close_board(gui, save=False)
    assert res["ok"] and res["closed"] is True and res["mode"] == "gui"
    assert res["saved"] is False and res["discarded_changes"] is False
    assert "stays in the KiCad editor" in res["note"] and "save or undo it there" in res["note"]
    assert fake_api.opened[0].closed and brt.list_open_boards()["boards"] == []
    brt.route_net(gui, "SIG", SIG_PATH)
    assert "note" not in brt.close_board(gui)               # saved: nothing left behind


def test_close_board_keeps_the_board_open_when_the_save_fails(pcb):
    brt.route_net(pcb, "SIG", SIG_PATH)

    def broken():
        raise BoardApiError("saving failed: disk full")

    _api().save = broken
    res = brt.close_board(pcb)
    assert res["ok"] is False and "board left open" in res["reason"] and "disk full" in res["reason"]
    assert brt.list_open_boards()["boards"][0]["path"] == pcb and not _api().closed


# ============================================================================
# the pre-KiCad-11 snapshot (ROADMAP D5)
# ============================================================================
@pytest.fixture
def k9_pcb(tmp_path) -> str:
    """A board built by KiCad 9's pcbnew, with its project: what agent routing gets."""
    path = tmp_path / "board.kicad_pcb"
    path.write_text(KICAD9_HEADER, encoding="utf-8")
    (tmp_path / "board.kicad_pro").write_text('{"net_settings": {"classes": []}}\n',
                                              encoding="utf-8")
    return str(path)


@pytest.fixture
def k9_gui(tmp_path) -> str:
    """A KiCad 9-format board the (fake) running GUI has open."""
    path = tmp_path / "gui.kicad_pcb"
    path.write_text(KICAD9_HEADER, encoding="utf-8")
    return str(path)


def _taken(monkeypatch) -> list[str]:
    """Every toolchain.take_snapshot call (the real copy still runs)."""
    taken: list[str] = []
    real = tc.take_snapshot
    monkeypatch.setattr(tc, "take_snapshot", lambda path: taken.append(path) or real(path))
    return taken


def _clean_drc(monkeypatch) -> None:
    _drc_writer(monkeypatch, {"violations": [], "unconnected_items": []})


def test_first_headless_save_snapshots_once_before_writing(k9_pcb, kicad11, monkeypatch):
    taken = _taken(monkeypatch)
    _clean_drc(monkeypatch)
    snap = tc.pre_k11_snapshot_path(k9_pcb)
    opened = brt.open_board(k9_pcb)                         # headless: opening writes nothing
    assert opened["ok"] and "pre_k11_snapshot" not in opened and taken == []
    res = brt.route_check(k9_pcb)                           # the refill dirties it: saved
    assert res["saved"] is True and res["pre_k11_snapshot_taken"] == snap
    assert res["pre_k11_snapshot"] == snap and taken == [k9_pcb]
    assert _api().snapshot_at_save == [True]                # the copy preceded the save
    assert Path(snap).read_text(encoding="utf-8") == KICAD9_HEADER
    assert Path(k9_pcb).read_text(encoding="utf-8") == KICAD11_HEADER
    assert Path(tc.pre_k11_snapshot_path(k9_pcb)).with_suffix(".kicad_pro").is_file()
    brt.route_net(k9_pcb, "SIG", SIG_PATH)
    saved = brt.save_board(k9_pcb)
    assert saved["ok"] and saved["pre_k11_snapshot"] == snap and "pre_k11_snapshot_taken" not in saved
    brt.route_net(k9_pcb, "PWR", PWR_PATH)
    closed = brt.close_board(k9_pcb)
    assert closed["saved"] is True and closed["pre_k11_snapshot"] == snap
    assert "pre_k11_snapshot_taken" not in closed and taken == [k9_pcb]


def test_save_board_and_close_board_snapshot_an_untouched_kicad9_file(k9_pcb, tmp_path,
                                                                      monkeypatch):
    taken = _taken(monkeypatch)
    brt.open_board(k9_pcb)
    saved = brt.save_board(k9_pcb)                          # save_board always writes
    assert saved["pre_k11_snapshot_taken"] == tc.pre_k11_snapshot_path(k9_pcb)
    other = tmp_path / "other.kicad_pcb"
    other.write_text(KICAD9_HEADER, encoding="utf-8")
    brt.route_net(str(other), "SIG", SIG_PATH)              # releases the idle k9_pcb session
    closed = brt.close_board(str(other))
    assert closed["saved"] is True
    assert closed["pre_k11_snapshot_taken"] == tc.pre_k11_snapshot_path(str(other))
    assert taken == [k9_pcb, str(other)]


def test_kicad11_gui_open_snapshots_and_later_saves_do_not(k9_gui, monkeypatch):
    taken = _taken(monkeypatch)
    opened = brt.open_board(k9_gui)                         # the user may Ctrl+S at any time
    snap = tc.pre_k11_snapshot_path(k9_gui)
    assert opened["ok"] and opened["mode"] == "gui"
    assert opened["pre_k11_snapshot_taken"] == snap and opened["pre_k11_snapshot"] == snap
    assert taken == [k9_gui]
    brt.route_net(k9_gui, "SIG", SIG_PATH)
    assert brt.save_board(k9_gui)["pre_k11_snapshot"] == snap
    brt.close_board(k9_gui)
    again = brt.open_board(k9_gui)                          # the file is KiCad 11 now
    assert again["ok"] and "pre_k11_snapshot_taken" not in again and taken == [k9_gui]


def test_kicad9_sessions_never_snapshot(k9_gui, k9_pcb, kicad11, monkeypatch):
    FakeBoardApi.version = "9.0.4"
    taken = _taken(monkeypatch)
    _clean_drc(monkeypatch)
    assert brt.open_board(k9_gui)["ok"]
    brt.route_net(k9_gui, "SIG", SIG_PATH)
    assert "pre_k11_snapshot" not in brt.save_board(k9_gui)
    assert brt.route_check(k9_pcb)["saved"] is True
    assert taken == [] and Path(k9_pcb).read_text(encoding="utf-8") == KICAD9_HEADER


def test_a_failed_snapshot_refuses_the_save(k9_pcb, kicad11, monkeypatch):
    def full(path: str) -> dict:
        raise OSError("No space left on device")

    monkeypatch.setattr(tc, "take_snapshot", full)
    monkeypatch.setattr(tc, "run", lambda args, **kw: pytest.fail("DRC ran on an unsaved board"))
    res = brt.route_check(k9_pcb)
    assert res["ok"] is False and "pre-KiCad-11 snapshot" in res["reason"]
    assert "No space left" in res["reason"] and "not saved" in res["reason"]
    assert ("save",) not in _api().calls and _api().is_modified is True
    closed = brt.close_board(k9_pcb)
    assert closed["ok"] is False and "board left open" in closed["reason"]
    assert Path(k9_pcb).read_text(encoding="utf-8") == KICAD9_HEADER


def test_a_failed_snapshot_at_gui_open_refuses_the_session(k9_gui, monkeypatch, fake_api):
    def denied(path: str) -> dict:
        raise PermissionError("access denied")

    monkeypatch.setattr(tc, "take_snapshot", denied)
    res = brt.board_pads(k9_gui)
    assert res["ok"] is False and "pre-KiCad-11 snapshot" in res["reason"]
    assert "access denied" in res["reason"] and res["mode"] == "gui"
    assert fake_api.opened[0].closed and brt.list_open_boards()["boards"] == []


def test_the_snapshot_path_is_reported_by_the_read_and_list_tools(k9_pcb, monkeypatch):
    brt.open_board(k9_pcb)
    assert "pre_k11_snapshot" not in brt.board_overview(k9_pcb)
    brt.save_board(k9_pcb)
    snap = tc.pre_k11_snapshot_path(k9_pcb)
    assert brt.board_overview(k9_pcb)["pre_k11_snapshot"] == snap
    assert brt.list_open_boards()["boards"] == [{"path": k9_pcb, "mode": "headless",
                                                 "unsaved_changes": False,
                                                 "pre_k11_snapshot": snap}]


# ============================================================================
# route_check stall detection (ROADMAP D5)
# ============================================================================
def _round(monkeypatch, pcb: str, errors: int, unconnected: int, **kw) -> dict:
    _drc_writer(monkeypatch, {"violations": [CLEARANCE] * errors,
                              "unconnected_items": [UNCONNECTED] * unconnected})
    return brt.route_check(pcb, **kw)


def test_route_check_tracks_progress_and_flags_a_stall(pcb, kicad11, monkeypatch):
    first = _round(monkeypatch, pcb, 3, 2)
    assert first["progress"] == {"rounds": 1, "score": 5, "best": 5,
                                 "rounds_without_improvement": 0}
    assert first["stalled"] is False and "stall_note" not in first
    assert _round(monkeypatch, pcb, 2, 2)["progress"]["best"] == 4      # improved
    flat = [_round(monkeypatch, pcb, 4, 1), _round(monkeypatch, pcb, 2, 2),
            _round(monkeypatch, pcb, 3, 3)]                              # 5, 4, 6: none below 4
    assert [r["progress"]["rounds_without_improvement"] for r in flat] == [1, 2, 3]
    assert [r["stalled"] for r in flat] == [False, False, True]
    note = flat[-1]["stall_note"]
    assert "3 route_check rounds" in note and "autoroute_board" in note
    assert "from_snapshot=true" in note and "pack_scale" in note
    assert flat[-1]["progress"] == {"rounds": 5, "score": 6, "best": 4,
                                    "rounds_without_improvement": 3}
    better = _round(monkeypatch, pcb, 1, 0)
    assert better["stalled"] is False and better["progress"]["best"] == 1
    assert better["progress"]["rounds_without_improvement"] == 0


def test_a_clean_board_is_never_stalled(pcb, kicad11, monkeypatch):
    rounds = [_round(monkeypatch, pcb, 0, 0) for _ in range(5)]
    assert rounds[-1]["progress"]["rounds_without_improvement"] == 4
    assert rounds[-1]["ok"] is True and not any(r["stalled"] for r in rounds)


def test_rounds_that_judged_a_stale_file_do_not_count(pcb, kicad11, monkeypatch):
    _round(monkeypatch, pcb, 2, 0)
    brt.route_net(pcb, "SIG", SIG_PATH)                      # unsaved copper
    stale = _round(monkeypatch, pcb, 2, 0, refill=False, save=False)
    assert "progress" not in stale and "stalled" not in stale
    assert "file on disk" in stale["progress_note"]
    assert _round(monkeypatch, pcb, 2, 0)["progress"]["rounds"] == 2
    Path(pcb).with_name("board_drc.json").unlink()           # the last round's report
    monkeypatch.setattr(tc, "run", lambda args, **kw: _done())       # no report: no round
    failed = brt.route_check(pcb)
    assert failed["ok"] is False and "progress" not in failed
    assert _round(monkeypatch, pcb, 2, 0)["progress"]["rounds"] == 3


def test_progress_survives_a_reopen_and_restarts_after_a_rewrite(pcb, tmp_path, kicad11,
                                                                 monkeypatch):
    for _ in range(4):
        last = _round(monkeypatch, pcb, 1, 1)
    assert last["stalled"] is True
    brt.board_pads(str(tmp_path / "other.kicad_pcb"))        # releases the idle board
    assert _round(monkeypatch, pcb, 1, 1)["progress"]["rounds"] == 5
    assert brt.release_for_rewrite(pcb)["released_boards"] == [pcb]  # Freerouting / a rebuild
    fresh = _round(monkeypatch, pcb, 1, 1)
    assert fresh["progress"]["rounds"] == 1 and fresh["stalled"] is False
