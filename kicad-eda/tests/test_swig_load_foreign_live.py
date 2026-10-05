"""swig_load_foreign: foreign boards through pcbnew SWIG LoadBoard
(docs/ROADMAP_DECISIONS.md D0/D4; F46-03a).

Unit tier (no KiCad):
  * the stdlib OLE compound-file reader and the Altium property records it
    feeds (plane nets from Board6 / Polygons6 / Nets6), on compound files
    built here (mini-stream and regular-sector streams, a balanced sibling
    tree, a broken chain, a non-OLE file);
  * the script itself as a REAL subprocess of the venv Python against a stub
    pcbnew (STUB_PCBNEW below, steered by $STUB_PCBNEW_CFG): EAGLE refused on
    pcbnew 9 with code needs_newer_kicad BEFORE anything loads and allowed on
    10; LoadBoard always gets a copy in a temp dir, so an importer side file
    (the Eagle importer's <stem>.kicad_dru) never lands beside the source;
    inner_plane_loss (Altium on 9 only, fail-closed when unprovable),
    zones_on_disabled_layers for every plugin, and a board whose zone API is
    missing (diagnostics_errors, never a crash).

Live tier (skips cleanly without the install or $KICAD_EDA_FOREIGN_SAMPLES;
the samples are read from that directory directly and never downloaded):
  * KiCad 9.0.4 (local): eDP_adapter_dvt1.PcbDoc reports inner_plane_loss
    {zones 4, nets GND/P1.8VA/P1.8VD/P3.3V}; EasyEDA pcb_watch.json reports
    zones on In1.Cu / In2.Cu of a 2-layer board; Qwiic_Button.brd is refused
    with needs_newer_kicad;
  * KiCad 10.0.5 (Docker kicad-eda:wave1, samples mounted read-only): EAGLE
    Qwiic_Button loads 48 footprints and nothing is written beside the source.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import struct
import subprocess
import sys
import types
from pathlib import Path
from typing import Callable

import pytest

import kicad_env as env

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "server" / "tools" / "swig_load_foreign.py"
SAMPLES_ENV = "KICAD_EDA_FOREIGN_SAMPLES"
LIVE_TIMEOUT_S = 600
# stub_run(cfg, src, plugin, out=None) -> (proc, result, pcbnew calls)
StubRun = Callable[..., tuple[subprocess.CompletedProcess, dict, list[dict]]]
NEEDS_NEWER_HINT = ("LoadBoard(EAGLE) returns None on KiCad 9: use kicad-cli pcb import "
                    "(10.0+) or the 10.99 import")
ERROR_LINE = re.compile(r"(^|\s)Error:", re.MULTILINE)

# ---------------------------------------------------------------------------
# A version-3 OLE compound file writer (the container of an Altium .PcbDoc)
# ---------------------------------------------------------------------------
MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
FREESECT, ENDOFCHAIN, FATSECT, NOSTREAM = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFF


def _append_chain(blocks: list[bytes], table: list[int], data: bytes, size: int,
                  pad: bytes = b"\0") -> int:
    """Append `data` as a chain of `size`-byte blocks; returns the first id."""
    if not data:
        return ENDOFCHAIN
    first = len(blocks)
    pieces = [data[i:i + size] for i in range(0, len(data), size)]
    for n, piece in enumerate(pieces):
        blocks.append(piece.ljust(size, pad))
        table.append(first + n + 1 if n + 1 < len(pieces) else ENDOFCHAIN)
    return first


def _dir_entry(name: str, kind: int, left: int = NOSTREAM, right: int = NOSTREAM,
               child: int = NOSTREAM, start: int = 0, size: int = 0) -> bytes:
    raw = name.encode("utf-16-le") + b"\0\0" if name else b""
    return struct.pack("<64sHBBIII16sIQQIQ", raw, len(raw), kind, 1, left, right, child,
                       b"\0" * 16, 0, 0, 0, start, size)


def _link_balanced(entries: list[list], ids: list[int]) -> int:
    """Make `ids` a balanced sibling tree (left AND right pointers used)."""
    if not ids:
        return NOSTREAM
    mid = len(ids) // 2
    entries[ids[mid]][2] = _link_balanced(entries, ids[:mid])
    entries[ids[mid]][3] = _link_balanced(entries, ids[mid + 1:])
    return ids[mid]


def build_cfb(streams: dict[str, bytes], cutoff: int = 4096) -> bytes:
    """A minimal v3 compound file (512-byte sectors, one FAT sector) holding
    {"Storage/Stream": bytes}; streams shorter than `cutoff` go to the mini
    stream (64-byte mini sectors), the others to regular sectors."""
    sectors, fat = [b"\0" * 512], [FATSECT]          # sector 0 is the FAT itself
    mini, minifat = [], []
    entries: list[list] = [["Root Entry", 5, NOSTREAM, NOSTREAM, NOSTREAM, 0, 0]]
    storages: list[int] = []
    for storage in sorted({p.split("/")[0] for p in streams}):
        sid = len(entries)
        entries.append([storage, 1, NOSTREAM, NOSTREAM, NOSTREAM, 0, 0])
        storages.append(sid)
        kids = []
        for path in sorted(p for p in streams if p.split("/")[0] == storage):
            data = streams[path]
            start = _append_chain(mini, minifat, data, 64) if len(data) < cutoff else \
                _append_chain(sectors, fat, data, 512)
            kids.append(len(entries))
            entries.append([path.split("/")[1], 2, NOSTREAM, NOSTREAM, NOSTREAM, start,
                            len(data)])
        entries[sid][4] = _link_balanced(entries, kids)
    entries[0][4] = _link_balanced(entries, storages)
    ministream = b"".join(mini)
    entries[0][5] = _append_chain(sectors, fat, ministream, 512)
    entries[0][6] = len(ministream)
    minifat_raw = struct.pack(f"<{len(minifat)}I", *minifat)
    minifat_start = _append_chain(sectors, fat, minifat_raw, 512, pad=b"\xff")
    directory = b"".join(_dir_entry(*e) for e in entries)
    directory += _dir_entry("", 0) * (-len(entries) % 4)
    dir_start = _append_chain(sectors, fat, directory, 512)
    assert len(fat) <= 128, "the test writer has a single FAT sector"
    sectors[0] = struct.pack("<128I", *(fat + [FREESECT] * (128 - len(fat))))
    header = struct.pack("<8s16sHHHHH6sIIIIIIIII", MAGIC, b"\0" * 16, 0x3E, 3, 0xFFFE, 9, 6,
                         b"\0" * 6, 0, 1, dir_start, 0, cutoff, minifat_start,
                         -(-len(minifat_raw) // 512), ENDOFCHAIN, 0)
    header += struct.pack("<109I", 0, *([FREESECT] * 108))
    return header + b"".join(sectors)


def altium_record(props: dict[str, str | bytes], binary: bool = False) -> bytes:
    """One Altium property record: <u32 length>|KEY=VALUE|...\\0."""
    body = b"".join(b"|" + k.encode("latin-1") + b"=" +
                    (v if isinstance(v, bytes) else v.encode("latin-1"))
                    for k, v in props.items()) + b"\0"
    return struct.pack("<I", len(body) | (0x01000000 if binary else 0)) + body


NETS = ["GND", "P3V3", "P1V8", "SIG", "VSTALE"]
BOARD6 = {"FILENAME": "x.PcbDoc", "PLANE1NETNAME": "GND", "PLANE2NETNAME": "(Multiple Nets)",
          "PLANE3NETNAME": "VSTALE", "PLANE4NETNAME": "(No Net)",
          "LAYER1NEXT": "39", "LAYER39NEXT": "40", "LAYER40NEXT": "32", "LAYER32NEXT": "0"}
POLYGONS = [
    {"LAYER": "PLANE2", "POLYGONTYPE": "Split Plane", "NET": "1"},
    {"LAYER": "PLANE2", "POLYGONTYPE": "Split Plane", "NET": "2", "VX0": "1" * 5000},
    {"LAYER": "TOP", "POLYGONTYPE": "Polygon", "NET": "3"},          # not a plane
    {"LAYER": "PLANE5", "POLYGONTYPE": "Split Plane", "NET": "3"},   # plane not stacked
    {"LAYER": "PLANE1", "POLYGONTYPE": "Split Plane", "NET": "65535"},  # no net
]


def altium_pcbdoc(board6: dict | None = None, polygons: list | None = None,
                  nets: list[str] | None = None, cutoff: int = 4096) -> bytes:
    """A compound file holding the three streams the plane-net check reads."""
    streams = {
        "Board6/Data": altium_record(BOARD6 if board6 is None else board6),
        "Nets6/Data": b"".join(altium_record({"LAYER": "TOP", "NAME": n})
                               for n in (NETS if nets is None else nets)),
        "FileHeader/Data": b"\x1bPCB 6.0 Binary File",
    }
    polys = POLYGONS if polygons is None else polygons
    if polys:
        streams["Polygons6/Data"] = b"".join(altium_record(p) for p in polys) + \
            altium_record({"BINARY": "x"}, binary=True)
    return build_cfb(streams, cutoff)


# ---------------------------------------------------------------------------
# The module in-process, against a bare pcbnew stand-in (pure functions only)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def slf() -> types.ModuleType:
    stub = types.ModuleType("pcbnew")
    stub.PCB_IO_MGR = types.SimpleNamespace(ALTIUM_DESIGNER=5, EAGLE=7, EASYEDA=8,
                                            EASYEDAPRO=9)
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "pcbnew", stub)
        spec = importlib.util.spec_from_file_location("swig_load_foreign_under_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module


@pytest.mark.parametrize(("text", "major"), [
    ("9.0.4", 9), ("10.0.5", 10), ("(10.0.5-1)", 10), ("10.99.0-4025-gf7ca62", 10),
    ("", 0), ("unknown", 0)])
def test_pcbnew_major(slf: types.ModuleType, text: str, major: int) -> None:
    assert slf.pcbnew_major(text) == major


def test_plugin_type_resolves_names_and_refuses(slf: types.ModuleType) -> None:
    assert slf.plugin_type("ALTIUM_DESIGNER", 9) == 5
    assert slf.plugin_type("EASYEDA", 10) == 8
    assert slf.plugin_type("EAGLE", 10) == 7                 # the name, resolved in the child
    with pytest.raises(slf.Refused) as eagle9:
        slf.plugin_type("EAGLE", 9)
    assert eagle9.value.code == "needs_newer_kicad"
    assert "EAGLE refused" in str(eagle9.value) and NEEDS_NEWER_HINT in str(eagle9.value)
    with pytest.raises(slf.Refused) as unknown:
        slf.plugin_type("PADS", 10)
    assert unknown.value.code == "unknown_plugin" and "not one of" in str(unknown.value)
    with pytest.raises(slf.Refused) as pro:
        slf.plugin_type("EASYEDAPRO", 10)
    assert pro.value.code == "unknown_plugin"


@pytest.mark.parametrize("cutoff", [4096, 0])     # 0: every stream in regular sectors
def test_compound_file_reads_streams(slf: types.ModuleType, cutoff: int) -> None:
    big, small = bytes(range(256)) * 40, b"tiny stream"
    cfb = slf.CompoundFile(build_cfb({"A/Data": small, "B/Data": big, "B/Header": b"h" * 70,
                                      "C/Data": b"", "D/Data": b"d" * 4096}, cutoff))
    assert cfb.stream("A/Data") == small and cfb.stream("b/data") == big
    assert cfb.stream("B/Header") == b"h" * 70 and cfb.stream("C/Data") == b""
    assert cfb.stream("D/Data") == b"d" * 4096
    assert cfb.has("A/Data") and not cfb.has("A/Missing") and not cfb.has("Z/Data")
    with pytest.raises(KeyError, match="Z/Data"):
        cfb.stream("Z/Data")
    with pytest.raises(ValueError, match="not a stream"):
        cfb.stream("A")


def test_compound_file_rejects_garbage(slf: types.ModuleType) -> None:
    with pytest.raises(ValueError, match="not an OLE compound file"):
        slf.CompoundFile(b"\xd0\xcf\x11\xe0 fake OLE")
    data = bytearray(build_cfb({"A/Data": b"x" * 5000}, 4096))
    fat_at = 512                                    # sector 0 is the FAT; A/Data starts at 1
    assert struct.unpack_from("<I", data, fat_at + 4)[0] == 2      # 1 -> 2 -> ...
    struct.pack_into("<I", data, fat_at + 4 * 2, 1)                 # 2 -> 1: a cycle
    with pytest.raises(ValueError, match="cyclic|broken"):
        slf.CompoundFile(bytes(data)).stream("A/Data")
    truncated = build_cfb({"A/Data": b"x" * 5000})[:1024]
    with pytest.raises(ValueError):
        slf.CompoundFile(truncated).stream("A/Data")


def test_altium_records(slf: types.ModuleType) -> None:
    raw = altium_record({"NAME": "GND", "layer": "TOP"}) + \
        altium_record({"BINARY": "1"}, binary=True) + \
        altium_record({"NAME": b"V?A", "%UTF8%NAME": "VµA".encode("utf-8")}) + \
        altium_record({"%UTF8%NAME": "Ω1".encode("utf-8"), "NAME": b"?1"}) + \
        struct.pack("<I", 4) + b"|A=1"                       # no trailing NUL
    recs = slf.altium_records(raw)
    assert recs == [{"NAME": "GND", "LAYER": "TOP"}, {"NAME": "VµA"}, {"NAME": "Ω1"},
                    {"A": "1"}]
    with pytest.raises(ValueError, match="truncated"):
        slf.altium_records(struct.pack("<I", 50) + b"|A=1")


def test_altium_plane_nets(slf: types.ModuleType) -> None:
    # PLANE1 GND + the PLANE2 split planes; VSTALE sits on PLANE3 (not stacked)
    assert slf.altium_plane_nets(altium_pcbdoc()) == {"GND", "P3V3", "P1V8"}
    assert slf.altium_plane_nets(altium_pcbdoc(cutoff=0)) == {"GND", "P3V3", "P1V8"}
    # no LAYERnNEXT stack: every plane counts (VSTALE too, and PLANE5's SIG)
    flat = {k: v for k, v in BOARD6.items() if "NEXT" not in k}
    assert slf.altium_plane_nets(altium_pcbdoc(board6=flat)) == \
        {"GND", "P3V3", "P1V8", "VSTALE", "SIG"}
    # no Polygons6 stream at all: the Board6 plane nets alone
    assert slf.altium_plane_nets(altium_pcbdoc(polygons=[])) == {"GND"}
    # a split plane whose net index is out of range is ignored
    assert slf.altium_plane_nets(altium_pcbdoc(nets=["GND"])) == {"GND"}
    with pytest.raises(ValueError):
        slf.altium_plane_nets(b"not a compound file")


# ---------------------------------------------------------------------------
# The script as a subprocess against a stub pcbnew
# ---------------------------------------------------------------------------
STUB_PCBNEW = '''\
import json
import os
import sys

QUIET_FIRST = "kicad_quiet" in sys.modules
CFG = json.loads(os.environ.get("STUB_PCBNEW_CFG") or "{}")
F_Cu, B_Cu, Eco1_User = 0, 2, 21
NAMES = {0: "F.Cu", 2: "B.Cu", 21: "Eco1.User"}
for _k in range(1, 31):
    globals()["In%d_Cu" % _k] = 2 * _k + 2
    NAMES[2 * _k + 2] = "In%d.Cu" % _k
IDS = {name: layer for layer, name in NAMES.items()}
K10 = CFG.get("version", "9.0.4").startswith("10")


class PCB_IO_MGR:                       # 10.0 inserted ALLEGRO=3 and shifted the rest
    ALTIUM_DESIGNER, EAGLE, EASYEDA, EASYEDAPRO = (6, 8, 9, 10) if K10 else (5, 7, 8, 9)


def IsCopperLayer(layer):
    return layer in (0, 2) or (4 <= layer <= 62 and layer % 2 == 0)


def Version():
    return CFG.get("version", "9.0.4")


class _LSet:
    def __init__(self, layers):
        self._layers = layers

    def Seq(self):
        return tuple(self._layers)


class _Zone:
    def __init__(self, spec):
        self._spec = spec

    def GetIsRuleArea(self):
        return bool(self._spec.get("rule_area"))

    def GetLayerSet(self):
        return _LSet([IDS[name] for name in self._spec["layers"]])

    def GetNetname(self):
        return self._spec.get("net", "")


class _Item:
    def __init__(self, cls):
        self._cls = cls

    def GetClass(self):
        return self._cls


class _BareBoard:                       # no zone API: the diagnostics must not crash
    def GetFootprints(self):
        return [object()] * 3

    def GetTracks(self):
        return [_Item("PCB_TRACK")] * 4 + [_Item("PCB_VIA")] * 2

    def GetNetCount(self):
        return 7

    def GetAreaCount(self):
        return len(CFG.get("zones", []))

    def GetCopperLayerCount(self):
        return CFG.get("copper", 2)


class BOARD(_BareBoard):
    @staticmethod
    def GetStandardLayerName(layer):
        return NAMES[layer]

    def Zones(self):
        return [_Zone(z) for z in CFG.get("zones", [])]


def _log(**fields):
    with open(os.environ["STUB_PCBNEW_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps(fields) + "\\n")


def LoadBoard(path, kind=None):
    _log(call="LoadBoard", path=path, kind=kind, quiet_first=QUIET_FIRST,
         head=open(path, "rb").read(16).hex())
    if CFG.get("side_file"):            # the Eagle importer: <stem>.kicad_dru beside its input
        with open(os.path.splitext(path)[0] + ".kicad_dru", "w") as f:
            f.write("(version 1)\\n")
    load = CFG.get("load", "board")
    if load == "none":
        return None
    if load == "raise":
        raise OSError("stub: cannot read the board file")
    return _BareBoard() if load == "bare" else BOARD()


def SaveBoard(path, board):
    _log(call="SaveBoard", path=path)
    with open(path, "w", encoding="utf-8") as f:
        f.write("(kicad_pcb (version 20241229))\\n")
    return True
'''


@pytest.fixture
def stub_run(tmp_path: Path) -> StubRun:
    """run(cfg, src, plugin) -> (proc, result, calls): the real script under
    the venv Python with STUB_PCBNEW configured by `cfg`."""
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "pcbnew.py").write_text(STUB_PCBNEW, encoding="utf-8")
    log = tmp_path / "pcbnew_calls.jsonl"

    def run(cfg: dict, src: Path, plugin: str, out: Path | None = None
            ) -> tuple[subprocess.CompletedProcess, dict, list[dict]]:
        out = out or tmp_path / "out" / "board.kicad_pcb"
        out.parent.mkdir(parents=True, exist_ok=True)
        child_env = {**os.environ, "PYTHONPATH": str(stub), "STUB_PCBNEW_LOG": str(log),
                     "STUB_PCBNEW_CFG": json.dumps(cfg)}
        proc = subprocess.run([sys.executable, str(SCRIPT), str(src), str(out), plugin],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", stdin=subprocess.DEVNULL, timeout=60,
                              env=child_env)
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] \
            if log.is_file() else []
        return proc, result, calls
    return run


def listing(directory: Path) -> dict[str, int]:
    return {p.name: p.stat().st_mtime_ns for p in directory.iterdir()}


def _source(tmp_path: Path, name: str, data: bytes = b"<eagle/>\n") -> Path:
    src = tmp_path / "src" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(data)
    return src


def test_eagle_refused_on_pcbnew9_before_loading(tmp_path: Path, stub_run: StubRun) -> None:
    src = _source(tmp_path, "Qwiic_Button.brd")
    before = listing(src.parent)
    proc, res, calls = stub_run({"version": "9.0.4", "side_file": True}, src, "EAGLE")
    assert proc.returncode == 2 and not res["ok"]
    assert res["code"] == "needs_newer_kicad" and NEEDS_NEWER_HINT in res["error"]
    assert "EAGLE refused" in res["error"] and res["pcbnew"] == "9.0.4"
    assert calls == [] and listing(src.parent) == before
    assert not (tmp_path / "out" / "board.kicad_pcb").exists()


def test_eagle_refused_when_the_version_is_unreadable(tmp_path: Path, stub_run: StubRun) -> None:
    proc, res, calls = stub_run({"version": "unknown"}, _source(tmp_path, "b.brd"), "EAGLE")
    assert proc.returncode == 2 and res["code"] == "needs_newer_kicad" and calls == []


def test_eagle_loads_a_temp_copy_on_pcbnew10(tmp_path: Path, stub_run: StubRun) -> None:
    src = _source(tmp_path, "Qwiic_Button.brd")
    before = listing(src.parent)
    proc, res, calls = stub_run({"version": "10.0.5", "side_file": True}, src, "EAGLE")
    assert proc.returncode == 0 and res["ok"], (res, proc.stderr)
    load, save = calls
    assert load["call"] == "LoadBoard" and load["kind"] == 8 and load["quiet_first"]
    assert Path(load["path"]).name == src.name and Path(load["path"]).parent != src.parent
    assert load["head"] == src.read_bytes()[:16].hex()        # a byte-for-byte copy
    assert not Path(load["path"]).parent.exists()             # the temp dir is gone
    assert listing(src.parent) == before                      # no .kicad_dru beside the source
    assert res["importer_side_files"] == ["Qwiic_Button.kicad_dru"]
    assert save["path"] == res["output"] and Path(res["output"]).is_file()
    assert res["footprints"] == 3 and res["tracks"] == 4 and res["vias"] == 2
    assert res["inner_plane_loss"] is None and res["zones_on_disabled_layers"] == {}
    assert "diagnostics_errors" not in res and "code" not in res


def test_easyeda_zones_on_disabled_layers(tmp_path: Path, stub_run: StubRun) -> None:
    zones = [{"layers": ["In1.Cu"]}, {"layers": ["In2.Cu"]}, {"layers": ["In2.Cu"]},
             {"layers": ["F.Cu"], "net": "GND"}, {"layers": ["B.Cu", "In3.Cu"]},
             {"layers": ["In1.Cu"], "rule_area": True}]
    src = _source(tmp_path, "pcb_watch.json", b'{"head": {"docType": "3"}}')
    proc, res, _ = stub_run({"copper": 2, "zones": zones}, src, "EASYEDA")
    assert proc.returncode == 0 and res["ok"]
    assert res["zones_on_disabled_layers"] == {"In1.Cu": 1, "In2.Cu": 2, "In3.Cu": 1}
    assert res["inner_plane_loss"] is None                    # Altium-only check


def test_four_layer_board_inner_zones_are_enabled(tmp_path: Path, stub_run: StubRun) -> None:
    zones = [{"layers": ["In1.Cu"]}, {"layers": ["In2.Cu"]}, {"layers": ["In3.Cu"]}]
    src = _source(tmp_path, "w.json", b"{}")
    _, res, _ = stub_run({"copper": 4, "zones": zones, "version": "10.0.5"}, src, "EASYEDA")
    assert res["zones_on_disabled_layers"] == {"In3.Cu": 1}


ALTIUM9_ZONES = [{"layers": ["Eco1.User"]}, {"layers": ["Eco1.User"]},
                 {"layers": ["Eco1.User"]}, {"layers": ["Eco1.User"], "rule_area": True},
                 {"layers": ["Eco1.User"], "rule_area": True},
                 {"layers": ["F.Cu"], "net": "GND"}, {"layers": ["In1.Cu"], "net": "P3V3"}]


def test_altium_on_pcbnew9_reports_inner_plane_loss(tmp_path: Path, stub_run: StubRun) -> None:
    src = _source(tmp_path, "x.PcbDoc", altium_pcbdoc())
    proc, res, _ = stub_run({"copper": 4, "zones": ALTIUM9_ZONES}, src, "ALTIUM_DESIGNER")
    assert proc.returncode == 0 and res["ok"], res
    # 3 non-rule-area zones moved to Eco1; P3V3 still has In1.Cu copper
    assert res["inner_plane_loss"] == {"zones": 3, "nets": ["GND", "P1V8"]}
    assert res["zones_on_disabled_layers"] == {}


def test_altium_on_pcbnew9_without_loss(tmp_path: Path, stub_run: StubRun) -> None:
    zones = [{"layers": ["In1.Cu"], "net": "GND"}, {"layers": ["In2.Cu"], "net": "P3V3"},
             {"layers": ["In2.Cu"], "net": "P1V8"}, {"layers": ["Eco1.User"], "rule_area": True}]
    src = _source(tmp_path, "ok.PcbDoc", altium_pcbdoc())
    _, res, _ = stub_run({"copper": 4, "zones": zones}, src, "ALTIUM_DESIGNER")
    assert res["inner_plane_loss"] is None


def test_altium_plane_check_fails_closed(tmp_path: Path, stub_run: StubRun) -> None:
    """Unreadable plane nets with no Eco1 zone: the loss cannot be ruled out."""
    src = _source(tmp_path, "fake.PcbDoc", b"\xd0\xcf\x11\xe0 fake OLE")
    _, res, _ = stub_run({"copper": 4, "zones": []}, src, "ALTIUM_DESIGNER")
    loss = res["inner_plane_loss"]
    assert loss["zones"] == 0 and loss["nets"] is None
    assert "not an OLE compound file" in loss["error"]


def test_altium_on_pcbnew10_skips_the_plane_check(tmp_path: Path, stub_run: StubRun) -> None:
    src = _source(tmp_path, "x.PcbDoc", b"not even parsed")
    _, res, calls = stub_run({"version": "10.0.5", "copper": 4, "zones": ALTIUM9_ZONES},
                             src, "ALTIUM_DESIGNER")
    assert res["ok"] and res["inner_plane_loss"] is None and calls[0]["kind"] == 6


@pytest.mark.parametrize(("plugin", "errors"), [("ALTIUM_DESIGNER", 2), ("EASYEDA", 1)])
def test_missing_zone_api_is_reported_not_fatal(tmp_path: Path, stub_run: StubRun, plugin: str,
                                                errors: int) -> None:
    src = _source(tmp_path, "b.PcbDoc", altium_pcbdoc())
    proc, res, _ = stub_run({"load": "bare"}, src, plugin)
    assert proc.returncode == 0 and res["ok"] and res["zones_on_disabled_layers"] is None
    assert len(res["diagnostics_errors"]) == errors
    assert all("AttributeError" in e for e in res["diagnostics_errors"])
    if plugin == "ALTIUM_DESIGNER":                           # fail closed
        assert res["inner_plane_loss"]["zones"] is None and "Zones" in \
            res["inner_plane_loss"]["error"]
    else:
        assert res["inner_plane_loss"] is None


@pytest.mark.parametrize(("cfg", "code", "rc", "text"), [
    ({"load": "none"}, "load_returned_none", 1, "returned None"),
    ({"load": "raise"}, "load_raised", 1, "OSError: stub: cannot read"),
])
def test_load_failures_carry_codes(tmp_path: Path, stub_run: StubRun, cfg: dict,
                                   code: str, rc: int, text: str) -> None:
    proc, res, _ = stub_run(cfg, _source(tmp_path, "x.json", b"{}"), "EASYEDA")
    assert proc.returncode == rc and not res["ok"] and res["code"] == code
    assert text in res["error"]


def test_usage_errors_carry_codes(tmp_path: Path, stub_run: StubRun) -> None:
    src = _source(tmp_path, "x.json", b"{}")
    proc, res, _ = stub_run({}, tmp_path / "missing.json", "EASYEDA")
    assert proc.returncode == 1 and res["code"] == "input_not_found"
    proc, res, _ = stub_run({}, src, "EASYEDA", out=tmp_path / "out" / "x.pcb")
    assert proc.returncode == 2 and res["code"] == "usage"
    proc, res, _ = stub_run({}, src, "PADS")
    assert proc.returncode == 2 and res["code"] == "unknown_plugin"
    proc, res, _ = stub_run({}, tmp_path / "src", "EASYEDA")    # a directory
    assert proc.returncode == 1 and res["code"] == "input_not_found"


# ---------------------------------------------------------------------------
# Live tier: real pcbnew, real samples
# ---------------------------------------------------------------------------
def foreign_sample(*parts: str) -> Path:
    root = os.environ.get(SAMPLES_ENV, "").strip()
    if not root:
        pytest.skip(f"${SAMPLES_ENV} is not set (sample files are never downloaded by tests)")
    path = Path(root).joinpath(*parts)
    if not path.is_file():
        pytest.skip(f"sample file missing: {path}")
    return path


def swig_python(major: int) -> Path:
    for inst in env.discover():
        if inst.version_tuple[0] == major and inst.python is not None:
            return inst.python
    pytest.skip(f"no KiCad {major}.x install whose bundled Python imports pcbnew")


@pytest.fixture(scope="module")
def kicad9() -> Path:
    return swig_python(9)


@pytest.fixture(scope="module")
def kicad10() -> Path:
    return swig_python(10)


def run_live(python: Path, src: Path, out: Path,
             plugin: str) -> tuple[subprocess.CompletedProcess, dict]:
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run([str(python), str(SCRIPT), str(src), str(out), plugin],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", stdin=subprocess.DEVNULL,
                              timeout=LIVE_TIMEOUT_S, cwd=out.parent)
    except subprocess.TimeoutExpired as e:
        pytest.fail(f"swig_load_foreign hung for {LIVE_TIMEOUT_S} s (a modal dialog?): "
                    f"stderr={e.stderr!r}")
    assert proc.stdout.strip(), (proc.returncode, proc.stderr[-3000:])
    return proc, json.loads(proc.stdout.strip().splitlines()[-1])


def _saved_version(out: Path) -> int:
    head = out.read_text(encoding="utf-8", errors="replace")[:400]
    found = re.search(r"\(version (\d+)\)", head)
    assert found, head
    return int(found.group(1))


def test_live_k9_altium_reports_inner_plane_loss(kicad9: Path, tmp_path: Path) -> None:
    src = foreign_sample("foreign", "altium", "eDP_adapter_dvt1.PcbDoc")
    before, out = listing(src.parent), tmp_path / "edp.kicad_pcb"
    proc, res = run_live(kicad9, src, out, "ALTIUM_DESIGNER")
    assert proc.returncode == 0 and res["ok"], (res, proc.stderr[-3000:])
    assert res["pcbnew"].startswith("9.") and res["copper_layers"] == 4
    assert res["inner_plane_loss"] == {"zones": 4, "nets": ["GND", "P1.8VA", "P1.8VD",
                                                            "P3.3V"]}
    assert res["zones_on_disabled_layers"] == {} and "diagnostics_errors" not in res
    assert "moved to KiCad layer Eco1_User" in proc.stderr
    assert not ERROR_LINE.search(proc.stdout + proc.stderr), proc.stderr[-3000:]
    assert _saved_version(out) == 20241229
    assert listing(src.parent) == before


def test_live_k9_easyeda_zones_on_disabled_layers(kicad9: Path, tmp_path: Path) -> None:
    src = foreign_sample("foreign", "easyeda", "pcb_watch.json")
    before, out = listing(src.parent), tmp_path / "watch.kicad_pcb"
    proc, res = run_live(kicad9, src, out, "EASYEDA")
    assert proc.returncode == 0 and res["ok"], (res, proc.stderr[-3000:])
    assert res["copper_layers"] == 2
    assert res["zones_on_disabled_layers"] == {"In1.Cu": 1, "In2.Cu": 1}
    assert res["inner_plane_loss"] is None and "diagnostics_errors" not in res
    assert listing(src.parent) == before


def test_live_k9_refuses_eagle_with_needs_newer_kicad(kicad9: Path, tmp_path: Path) -> None:
    src = foreign_sample("eagle", "Qwiic_Button.brd")
    before, out = listing(src.parent), tmp_path / "qb.kicad_pcb"
    proc, res = run_live(kicad9, src, out, "EAGLE")
    assert proc.returncode == 2 and not res["ok"]
    assert res["code"] == "needs_newer_kicad" and NEEDS_NEWER_HINT in res["error"]
    assert res["pcbnew"].startswith("9.") and not out.exists()
    assert listing(src.parent) == before


def test_live_k10_eagle_loads_and_writes_nothing_beside_the_source(
        kicad10: Path, tmp_path: Path) -> None:
    src = foreign_sample("eagle", "Qwiic_Button.brd")
    before, out = listing(src.parent), tmp_path / "qb.kicad_pcb"
    proc, res = run_live(kicad10, src, out, "EAGLE")
    assert proc.returncode == 0 and res["ok"], (res, proc.stderr[-3000:])
    assert res["pcbnew"].startswith("10.")
    assert (res["footprints"], res["tracks"], res["vias"], res["zones"]) == (48, 193, 38, 2)
    assert "Qwiic_Button.kicad_dru" in res["importer_side_files"]
    assert "can't open file" not in proc.stderr, proc.stderr[-3000:]
    assert listing(src.parent) == before
    assert res["zones_on_disabled_layers"] == {} and res["inner_plane_loss"] is None
    assert _saved_version(out) == 20260206


def test_live_k10_altium_and_easyeda(kicad10: Path, tmp_path: Path) -> None:
    altium = foreign_sample("foreign", "altium", "eDP_adapter_dvt1.PcbDoc")
    proc, res = run_live(kicad10, altium, tmp_path / "a" / "edp.kicad_pcb", "ALTIUM_DESIGNER")
    assert proc.returncode == 0 and res["ok"], (res, proc.stderr[-3000:])
    assert res["inner_plane_loss"] is None and res["zones_on_disabled_layers"] == {}
    watch = foreign_sample("foreign", "easyeda", "pcb_watch.json")
    proc, res = run_live(kicad10, watch, tmp_path / "e" / "watch.kicad_pcb", "EASYEDA")
    assert proc.returncode == 0 and res["ok"], (res, proc.stderr[-3000:])
    assert res["zones_on_disabled_layers"] == {"In1.Cu": 1, "In2.Cu": 1}
