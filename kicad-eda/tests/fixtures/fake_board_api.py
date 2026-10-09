"""The fake board API the board_route_tools / board_sessions tests share.

FakeBoardApi is an in-memory stand-in for backends.pcb_api.BoardApi (the dict
shapes backends.pcb_items produces); its save() rewrites an existing board
file with a KiCad 11 header, as the real headless save does. The helpers
install it on board_sessions (the registry that opens boards), fake the only
KiCad install (a 10.99 nightly) and stand a fake toolchain.run in for
kicad-cli DRC. Test modules wrap them in their own fixtures.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest

import board_sessions as bs
import kicad_env as env
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


def pad(pid: str, ref: str, num: str, net: str, x: float, y: float, *,
        layers: tuple[str, ...] = ("F.Cu",), w: float = 1.0, h: float = 0.6,
        drill: float | None = None) -> dict:
    """One pad in the pcb_items shape."""
    return {"id": pid, "ref": ref, "pad": num, "net": net, "x": x, "y": y, "layers": list(layers),
            "through_hole": drill is not None, "shape": "roundrect", "w": w, "h": h,
            "rotation": 0.0, "drill": drill}


PADS = [pad("s1", "U1", "1", "SIG", 10.0, 10.0), pad("s2", "C1", "1", "SIG", 20.0, 10.0),
        pad("x1", "R1", "1", "OTHER", 15.0, 10.0),            # sits on the s1-s2 line
        pad("p1", "U1", "2", "PWR", 10.0, 20.0),
        pad("p2", "J1", "1", "PWR", 30.0, 20.0, layers=tuple(COPPER), w=1.7, h=1.7, drill=1.0),
        pad("g1", "C1", "2", "GND", 20.0, 12.0)]
SIG_PATH = [{"layer": "F.Cu", "points": [[10, 10], [14, 10]]}, {"via": [14, 10]},
            {"layer": "B.Cu", "points": [[14, 10], [20, 10]]}]
PWR_PATH = [{"layer": "F.Cu", "points": [[10, 20], [30, 20]]}]
CLEARANCE = {"type": "clearance", "severity": "error",
             "description": "Clearance violation (netclass 'Default' clearance 0.2000 mm; "
                            "actual 0.1000 mm)",
             "items": [{"description": "Track [SIG] on F.Cu", "pos": {"x": 12.5, "y": 10.0},
                        "uuid": "aa"},
                       {"description": "Pad 1 [OTHER] of R1 on F.Cu",
                        "pos": {"x": 15.0, "y": 10.0}, "uuid": "bb"}]}
UNCONNECTED = {"type": "unconnected_items", "severity": "error",
               "description": "Missing connection between items",
               "items": [{"description": "Pad 1 [P5V] of C1 on F.Cu",
                          "pos": {"x": 143.525, "y": 92.303}, "uuid": "cc"},
                         {"description": "PTH pad 1 [P5V] of J1",
                          "pos": {"x": 149.429, "y": 93.253}, "uuid": "dd"}]}


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


# ----------------------------------------------------------------------------
# helpers the test modules wrap in fixtures
# ----------------------------------------------------------------------------
def install(monkeypatch: pytest.MonkeyPatch) -> type[FakeBoardApi]:
    """board_sessions opens FakeBoardApi sessions into an empty registry."""
    FakeBoardApi.opened = []
    FakeBoardApi.unavailable = None
    FakeBoardApi.version = NIGHTLY
    monkeypatch.setattr(bs, "BoardApi", FakeBoardApi)
    monkeypatch.setattr(bs, "_BOARDS", {})
    monkeypatch.setattr(bs, "_PROGRESS", {})
    return FakeBoardApi


def board_file(tmp_path: Path, header: str = KICAD11_HEADER, name: str = "board.kicad_pcb") -> str:
    """A header-only board file (KiCad 11 by default)."""
    path = tmp_path / name
    path.write_text(header, encoding="utf-8")
    return str(path)


def kicad11(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> env.KiCadInstall:
    """The only install: a 10.99 nightly (kicad-cli 11 reads the KiCad 11 file)."""
    root = tmp_path / "10.99"
    inst = env.KiCadInstall(version="10.99", version_tuple=(10, 99, 0), root=root,
                            kicad_cli=root / "bin" / "kicad-cli.exe",
                            share=root / "share" / "kicad", has_api_server=True)
    monkeypatch.setattr(env, "discover", lambda refresh=False: [inst])
    return inst


def done(stdout: str = "", returncode: int = 0):
    """A finished child process as toolchain.run returns it."""
    return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": ""})()


def drc_writer(monkeypatch: pytest.MonkeyPatch, report: dict) -> list[list[str]]:
    """tc.run that records argv and writes `report` where kicad-cli would."""
    calls: list[list[str]] = []

    def fake_run(args, **kw):
        calls.append(list(args))
        Path(args[args.index("-o") + 1]).write_text(json.dumps(report), encoding="utf-8")
        return done()

    monkeypatch.setattr(tc, "run", fake_run)
    return calls


def drc_round(monkeypatch: pytest.MonkeyPatch, errors: int, unconnected: int) -> None:
    """The next kicad-cli DRC reports `errors` violations and `unconnected` items."""
    drc_writer(monkeypatch, {"violations": [CLEARANCE] * errors,
                             "unconnected_items": [UNCONNECTED] * unconnected})


def last() -> FakeBoardApi:
    """The most recently opened fake session."""
    return FakeBoardApi.opened[-1]
