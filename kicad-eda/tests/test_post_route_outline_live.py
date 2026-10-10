"""F3-16: post-route outline awareness (SES-import GND pour, on-board guard).

Unit tier (no KiCad; pcbnew stubbed for the import):
  * swig_copper.BoardEdge is the on-board guard: edge(point, clearance) is
    outline.contains of the sheet point in the board frame; keep(radius) binds
    radius + the copper-to-edge clearance (+ EDGE_MARGIN_MM);
  * clear_spot / clear_path / bfs_path take an optional on_board point
    predicate; without one they behave as before, with one a BFS maze detours
    round a guarded hole;
  * add_ep_via_farms skips grid spots the guard rejects;
  * line budgets: swig_board_fixes.py < 760 lines, swig_heal.py < 600.

Live tier (KiCad 9.0.4 locally, 10.0.5 in kicad-eda:wave1; skipped without a
KiCad 9/10 bundled Python that imports pcbnew). Boards are built with pcbnew;
the T10 outline (60 x 40, r=4 corners, a stadium slot and a 5 mm round hole)
sits at sheet (100.5, 115):
  (a) swig_import_ses.py (minimal SES) pours GND on F.Cu and B.Cu from the
      outline: 1 outline + 2 holes, plane_polyset's area, every ring >= 0.3 mm
      inside the edge (outline_audit), and the filled copper keeps the holes;
  (b) the healer links two pads whose straight path crosses the 5 mm hole by a
      maze route round it, and refuses when walls close the detour: no copper
      ends inside the hole, DRC shows no edge-clearance error. Control: without
      the guard the same heal lays copper straight through the hole;
  (c) on a rect board the pour is HEAD's Edge.Cuts-bbox rectangle, nm for nm;
  plus the bbox fallback with a warning (open Edge.Cuts), the LoadBoard checks
  and the parallel-via adder refusing a spot inside the hole's edge clearance.
"""
from __future__ import annotations

import importlib
import json
import math
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

import kicad_env as env
import outline as ol
import outline_audit as oa
from fixtures.swig_python import PREAMBLE, T10_BOARD

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "server" / "tools"
LIVE_TIMEOUT_S = 300
OX, OY = 100.5, 115.0                 # T10 / open boards: the board frame origin on the sheet
HOLE_C, HOLE_R = (30.0, 20.0), 2.5     # T10's round cutout (board frame)
POUR_INSET = 0.3
EDGE_TYPES = {"copper_edge_clearance", "edge_clearance"}


# ---------------------------------------------------------------------------
# Unit tier
# ---------------------------------------------------------------------------
class _Vec:
    def __init__(self, x: int, y: int) -> None:
        self.x, self.y = x, y


def _stub() -> types.ModuleType:
    stub = types.ModuleType("pcbnew")
    stub.ToMM = lambda nm: nm / 1e6
    stub.FromMM = lambda v: int(round(v * 1e6))
    stub.F_Cu, stub.In1_Cu, stub.B_Cu = 0, 4, 2
    stub.PAD_ATTRIB_PTH, stub.VIATYPE_THROUGH = 1, 3
    stub.VECTOR2I = _Vec

    class PCB_VIA:  # noqa: N801  (pcbnew's name)
        def __init__(self, board: object) -> None:
            self.pos = None

        def SetPosition(self, p: _Vec) -> None:  # noqa: N802
            self.pos = (p.x / 1e6, p.y / 1e6)

        def __getattr__(self, name: str):        # SetWidth, SetDrill, SetNet, ...
            return lambda *a: None

    stub.PCB_VIA = PCB_VIA
    return stub


def _import_stubbed(name: str) -> types.ModuleType:
    """Import a pcbnew-using tools module with pcbnew stubbed for that import only."""
    try:
        import pcbnew  # noqa: F401  (KiCad's bundled python only)
    except ImportError:
        pass
    else:
        return importlib.import_module(name)
    mods = ("swig_board_fixes", "swig_heal", "swig_copper")
    saved = {m: sys.modules.pop(m) for m in (*mods, "pcbnew") if m in sys.modules}
    sys.modules["pcbnew"] = _stub()
    try:
        return importlib.import_module(name)
    finally:
        for m in (*mods, "pcbnew"):
            sys.modules.pop(m, None)
        sys.modules.update(saved)


@pytest.fixture(scope="module")
def cu() -> types.ModuleType:
    return _import_stubbed("swig_copper")


@pytest.fixture(scope="module")
def t10_edge(cu) -> object:
    return cu.BoardEdge(ol.from_board_block(T10_BOARD), (OX, OY), 0.5)


def _sheet(x: float, y: float) -> tuple[float, float]:
    return (OX + x, OY + y)


class _Box:
    def __init__(self, x0: float, y0: float, x1: float, y1: float) -> None:
        self.v = [round(v * 1e6) for v in (x0, y0, x1, y1)]

    def GetLeft(self) -> int:  # noqa: N802
        return self.v[0]

    def GetTop(self) -> int:  # noqa: N802
        return self.v[1]

    def GetRight(self) -> int:  # noqa: N802
        return self.v[2]

    def GetBottom(self) -> int:  # noqa: N802
        return self.v[3]


class _EmptyBoard:
    """No copper at all: only the guard can block a spot."""

    def __init__(self, box: _Box) -> None:
        self.box, self.added = box, []

    def GetTracks(self) -> list:  # noqa: N802
        return []                  # what Add() receives is recorded, not copper

    def GetFootprints(self) -> list:  # noqa: N802
        return []

    def GetBoardEdgesBoundingBox(self) -> _Box:  # noqa: N802
        return self.box

    def Add(self, item: object) -> None:  # noqa: N802
        self.added.append(item)


def test_board_edge_is_outline_contains_in_the_board_frame(cu, t10_edge) -> None:
    assert t10_edge(_sheet(10, 20), 0.0)                      # material
    assert not t10_edge(_sheet(*HOLE_C), 0.0)                 # inside the 5 mm hole
    assert not t10_edge(_sheet(-1, 20), 0.0)                  # off the board
    near = _sheet(HOLE_C[0], HOLE_C[1] + HOLE_R + 0.6)        # 0.6 mm below the hole
    assert t10_edge(near, 0.59) and not t10_edge(near, 0.61)
    assert t10_edge.keep(0.05)(near) and not t10_edge.keep(0.1)(near)   # + 0.5 + margin
    assert cu.EDGE_MARGIN_MM == pytest.approx(0.01)
    assert cu.edge_keep(None, 0.3) is None
    assert cu.edge_keep(t10_edge, 0.05)(near) is True


def test_clear_spot_and_clear_path_take_an_optional_guard(cu) -> None:
    board = _EmptyBoard(_Box(0, 0, 40, 30))
    assert cu.clear_spot(board, 1, (5, 5), 0.5)                         # unchanged default
    assert cu.clear_spot(board, 1, (5, 5), 0.5, on_board=lambda p: True)
    assert not cu.clear_spot(board, 1, (5, 5), 0.5, on_board=lambda p: False)
    wall = lambda p: p[0] < 9.95 or p[0] > 10.05                      # noqa: E731
    assert cu.clear_path(board, 1, (5, 5), (15, 5), 0.5)
    assert not cu.clear_path(board, 1, (5, 5), (15, 5), 0.5, step=0.05, on_board=wall)
    assert cu.clear_path(board, 1, (5, 5), (9, 5), 0.5, on_board=wall)


def test_bfs_path_detours_round_a_guarded_hole(cu, t10_edge) -> None:
    board = _EmptyBoard(_Box(OX - 0.05, OY - 0.05, OX + 60.05, OY + 40.05))
    a, b = _sheet(23, 20), _sheet(37, 20)
    assert cu.bfs_path(board, 1, a, b, 0, 0.5) == [a, b]               # no guard: straight
    path = cu.bfs_path(board, 1, a, b, 0, 0.5, on_board=t10_edge.keep(0.15))
    assert path and path[0] == a and path[-1] == b and len(path) > 2
    need = HOLE_R + 0.15 + 0.5                                         # copper + edge clearance
    for p, q in zip(path, path[1:], strict=False):
        for k in range(101):
            s = (p[0] + (q[0] - p[0]) * k / 100 - OX, p[1] + (q[1] - p[1]) * k / 100 - OY)
            assert math.dist(s, HOLE_C) >= need - 0.01, (s, path)


def test_ep_via_farm_skips_spots_the_guard_rejects(cu, monkeypatch) -> None:
    fixes = _import_stubbed("swig_board_fixes")
    monkeypatch.setattr(importlib.import_module("thermal_check"), "check",
                        lambda design: {"ep_via_parts": ["U1"]})
    box = _Box(10, 10, 14, 14)
    box.GetWidth = lambda: box.v[2] - box.v[0]
    box.GetHeight = lambda: box.v[3] - box.v[1]
    box.Contains = lambda p: False
    net = types.SimpleNamespace(GetNetname=lambda: "GND")
    ep = types.SimpleNamespace(GetBoundingBox=lambda: box, GetNet=lambda: net,
                               GetDrillSize=lambda: _Vec(0, 0), GetPosition=lambda: _Vec(0, 0))
    fp = types.SimpleNamespace(Pads=lambda: [ep])

    def farm(on_board: object) -> list:
        board = _EmptyBoard(_Box(0, 0, 40, 30))
        board.FindFootprintByReference = lambda ref: fp
        applied = fixes.add_ep_via_farms(board, {}, on_board)
        return applied, sorted(v.pos for v in board.added)

    applied, all_vias = farm(None)
    assert len(all_vias) == 9 and applied == [
        "U1: 9 thermal via(s) 0.3 mm drill under the EP on net GND (IPC-7093 via farm)"]
    left = cu.BoardEdge(ol.Outline(ol.rect_loop(12.0, 30.0)), (0.0, 0.0), 0.0)
    applied, kept = farm(left)
    assert kept == [p for p in all_vias if p[0] + 0.3 + 0.01 <= 12.0] and len(kept) == 3
    assert applied == ["U1: 3 thermal via(s) 0.3 mm drill under the EP on net GND "
                       "(IPC-7093 via farm)"]


def test_line_budgets_of_the_post_route_modules() -> None:
    lines = {n: len((TOOLS / n).read_text(encoding="utf-8").splitlines())
             for n in ("swig_board_fixes.py", "swig_heal.py", "swig_copper.py",
                       "swig_import_ses.py")}
    assert lines["swig_board_fixes.py"] < 760 and lines["swig_heal.py"] < 600, lines
    assert max(lines.values()) < 800, lines


# ---------------------------------------------------------------------------
# Live tier
# ---------------------------------------------------------------------------
BUILD = PREAMBLE + r'''
import board_outline as bo
import board_shapes as bs
import outline as ol

mm, xy = pcbnew.FromMM, pcbnew.VECTOR2I
LAYER = {"F": pcbnew.F_Cu, "B": pcbnew.B_Cu}
ORIGIN = {"rect": (100.5, 115.25)}            # the drawn outlines (blocks) and "open"


def edges(board, kind, blocks):
    ox, oy = ORIGIN.get(kind, (100.5, 115.0))
    if kind in blocks:
        bo.draw_outline(board, ol.from_board_block(blocks[kind]), ox, oy)
    elif kind == "rect":
        bs.add_outline(board, 40, 30, ox, oy)
    else:                                   # three sides of 40 x 30: not closed
        for a, b in (((0, 0), (40, 0)), ((40, 0), (40, 30)), ((40, 30), (0, 30))):
            s = pcbnew.PCB_SHAPE(board)
            s.SetShape(pcbnew.SHAPE_T_SEGMENT)
            s.SetStart(xy(mm(a[0] + ox), mm(a[1] + oy)))
            s.SetEnd(xy(mm(b[0] + ox), mm(b[1] + oy)))
            s.SetLayer(pcbnew.Edge_Cuts)
            s.SetWidth(mm(0.1))
            board.Add(s)
    return ox, oy


def add_pad(fp, p, at, net):
    pad = pcbnew.PAD(fp)
    pad.SetNumber(p["num"])
    if p.get("pth"):
        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        pad.SetShape(pcbnew.F_Cu, pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetDrillSize(xy(mm(0.8), mm(0.8)))
        pad.SetLayerSet(pcbnew.PAD.PTHMask())
    else:
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetShape(pcbnew.F_Cu, pcbnew.PAD_SHAPE_RECT)
        pad.SetLayerSet(pcbnew.PAD.SMDMask())
    size = 1.6 if p.get("pth") else 1.0
    pad.SetSize(pcbnew.F_Cu, xy(mm(size), mm(size)))
    pad.SetPosition(at)
    pad.SetNet(net)
    fp.Add(pad)


def build(spec, out, blocks):
    board = pcbnew.CreateEmptyBoard()
    ox, oy = edges(board, spec["outline"], blocks)
    pt = lambda p: xy(mm(p[0] + ox), mm(p[1] + oy))
    nets = {}
    for name in spec["nets"]:
        nets[name] = pcbnew.NETINFO_ITEM(board, name)
        board.Add(nets[name])
    for ref, p in spec.get("pads", {}).items():
        fp = pcbnew.FOOTPRINT(board)
        fp.SetReference(ref)
        fp.SetPosition(pt(p["at"]))
        board.Add(fp)
        add_pad(fp, p, pt(p["at"]), nets[p["net"]])
    for t in spec.get("tracks", []):
        tr = pcbnew.PCB_TRACK(board)
        tr.SetStart(pt(t["a"]))
        tr.SetEnd(pt(t["b"]))
        tr.SetWidth(mm(t["w"]))
        tr.SetLayer(LAYER[t.get("layer", "F")])
        tr.SetNet(nets[t["net"]])
        board.Add(tr)
    for v in spec.get("vias", []):
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pt(v["at"]))
        via.SetWidth(mm(0.6))
        via.SetDrill(mm(0.3))
        via.SetNet(nets[v["net"]])
        board.Add(via)
    if not pcbnew.SaveBoard(out, board):
        raise SystemExit("SaveBoard failed: " + out)


specs, blocks = json.loads(open(sys.argv[1], encoding="utf-8").read()), json.loads(sys.argv[2])
for out, spec in specs.items():
    build(spec, out, blocks)
print(json.dumps({"built": sorted(specs)}))
'''

DUMP = PREAMBLE + r'''
import board_outline as bo
import outline as ol

to = pcbnew.ToMM


def rings(ps):
    out = []
    for i in range(ps.OutlineCount()):
        for ch in (ps.COutline(i), *(ps.CHole(i, h) for h in range(ps.HoleCount(i)))):
            out.append([[ch.CPoint(k).x, ch.CPoint(k).y] for k in range(ch.PointCount())])
    return out


def zone_rec(board, z):
    layer = z.GetLayer()
    fill = pcbnew.SHAPE_POLY_SET(z.GetFilledPolysList(layer)) if z.IsFilled() else None
    if fill is not None:
        fill.Unfracture()                   # stored fractured: holes slit into the outline
    return {"layer": board.GetLayerName(layer), "net": z.GetNetname(),
            "outlines": z.Outline().OutlineCount(),
            "holes": [z.Outline().HoleCount(i) for i in range(z.Outline().OutlineCount())],
            "rings": rings(z.Outline()), "area": z.Outline().Area() / 1e12,
            "filled_area": fill.Area() / 1e12 if fill is not None else 0.0,
            "filled_islands": fill.OutlineCount() if fill is not None else 0,
            "filled_holes": sum(fill.HoleCount(i) for i in range(fill.OutlineCount()))
            if fill is not None else 0,
            "filled_rings": rings(fill) if fill is not None else []}


def dump(pcb):
    board = bo.load_board(pcb)
    try:
        o, origin = bo.outline_from_board(board)
        block, plane = ol.to_board_block(o), bo.plane_polyset(board, 0.3).Area() / 1e12
    except bo.OutlineBuildError:
        block, origin, plane = None, None, None
    bb = board.GetBoardEdgesBoundingBox()       # HEAD's pour rectangle, verbatim
    x0, y0, w, h = to(bb.GetLeft()), to(bb.GetTop()), to(bb.GetWidth()), to(bb.GetHeight())
    i = 0.3
    legacy = [[pcbnew.FromMM(float(x)), pcbnew.FromMM(float(y))] for x, y in (
        (x0 + i, y0 + i), (x0 + w - i, y0 + i), (x0 + w - i, y0 + h - i), (x0 + i, y0 + h - i))]
    tracks, vias = [], []
    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA":
            p = t.GetPosition()
            vias.append([t.GetNetname(), round(to(p.x), 4), round(to(p.y), 4)])
        else:
            s, e = t.GetStart(), t.GetEnd()
            tracks.append([t.GetNetname(), board.GetLayerName(t.GetLayer()),
                           round(to(t.GetWidth()), 4),
                           [[round(to(s.x), 4), round(to(s.y), 4)],
                            [round(to(e.x), 4), round(to(e.y), 4)]]])
    return {"outline": block, "origin": origin, "plane_area": plane, "legacy_rect": legacy,
            "edge_clearance": bo.edge_clearance_mm(board),
            "zones": sorted((zone_rec(board, z) for z in board.Zones()),
                            key=lambda r: (r["layer"], r["net"])),
            "tracks": sorted(tracks), "vias": sorted(vias)}


print(json.dumps({pcb: dump(pcb) for pcb in sys.argv[1:]}))
'''

NO_GUARD = PREAMBLE + r'''
import swig_board_fixes as fixes
import swig_heal

pcb, drc_path, mode = sys.argv[1], sys.argv[2], sys.argv[3]
board = pcbnew.LoadBoard(pcb)
open_ = []
if mode == "heal":
    drc = json.loads(open(drc_path, encoding="utf-8").read())
    applied, open_ = swig_heal.heal_micro_gaps(board, drc, {})
else:
    applied = fixes.add_parallel_vias(board, {"net_specs": {"PWR": {"current": 2.0}}}, open_)
pcbnew.SaveBoard(pcb, board)
print(json.dumps({"applied": applied, "open": open_}))
'''

MIN_SES = """(session "board.ses"
  (base_design "board.dsn")
  (placement (resolution um 10))
  (was_is)
  (routes (resolution um 10)
    (parser (host_cad "KiCad's Pcbnew") (host_version "9.0"))
    (library_out)
    (network_out))
)
"""


def _pad(net: str, at: tuple, pth: bool = False) -> dict:
    return {"num": "1", "net": net, "at": list(at), "pth": pth}


def _trk(net: str, a: tuple, b: tuple, w: float) -> dict:
    return {"net": net, "a": list(a), "b": list(b), "w": w}


GND_PAD = {"G1": _pad("GND", (10, 15), pth=True)}
# two 20 x 30 lobes joined by a 0.5 mm neck: the 0.3 mm pour inset splits it in two
NECK_BOARD = {"outline": {"loop": [[0, 0], [20, 0], [20, 14.75], [25, 14.75], [25, 0], [45, 0],
                                   [45, 30], [25, 30], [25, 15.25], [20, 15.25], [20, 30],
                                   [0, 30]]}}
VIN_PADS = {"U1": _pad("VIN", (21, 20)), "U2": _pad("VIN", (39, 20))}
VIN_STUBS = [_trk("VIN", (21, 20), (23, 20), 0.3), _trk("VIN", (39, 20), (37, 20), 0.3)]
BOARDS = {
    "pour_t10": {"outline": "t10", "nets": ["GND"], "pads": GND_PAD},
    "pour_rect": {"outline": "rect", "nets": ["GND"], "pads": GND_PAD},
    "pour_neck": {"outline": "neck", "nets": ["GND"], "pads": GND_PAD},
    "pour_open": {"outline": "open", "nets": ["GND"], "pads": GND_PAD},
    "heal_around": {"outline": "t10", "nets": ["VIN"], "pads": VIN_PADS, "tracks": VIN_STUBS},
    # walls (single-track nets, so DRC sees no break on them) close every detour
    # in the maze window: slot-to-hole above it, hole-to-window-edge below it
    "heal_refuse": {"outline": "t10", "nets": ["VIN", "WALL1", "WALL2"], "pads": VIN_PADS,
                    "tracks": VIN_STUBS + [_trk("WALL1", (30, 14.65), (30, 16.85), 0.2),
                                           _trk("WALL2", (30, 23.15), (30, 26.5), 0.2)]},
    # a 2 A net whose only feed passes 0.65 mm under the hole: every parallel-via
    # spot on it (1.2 .. 2.6 mm from the via) is < via radius + 0.5 mm from the hole
    "vias_hole": {"outline": "t10", "nets": ["PWR"], "vias": [{"net": "PWR", "at": [28, 23.15]}],
                  "tracks": [_trk("PWR", (28, 23.15), (34, 23.15), 0.2)]},
}


@pytest.fixture(scope="module")
def kicad() -> env.KiCadInstall:
    """The oldest KiCad 9/10 install whose bundled Python imports pcbnew."""
    found = sorted((inst for inst in env.discover()
                    if inst.version_tuple[0] in (9, 10) and inst.python is not None),
                   key=lambda inst: inst.version_tuple)
    if not found:
        pytest.skip("no KiCad 9/10 bundled Python with pcbnew")
    return found[0]


def _run(argv: list) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                              timeout=LIVE_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        pytest.fail(f"KiCad child hung for {LIVE_TIMEOUT_S} s: {e.stdout!r} {e.stderr!r}")


def _last_json(done: subprocess.CompletedProcess) -> dict:
    return json.loads(done.stdout.strip().splitlines()[-1])


def _child(kicad: env.KiCadInstall, work: Path, script: str, *args) -> dict:
    path = work / f"child_{abs(hash(script)) % 10**8}.py"
    path.write_text(script, encoding="utf-8")
    done = _run([kicad.python, path, *args])
    assert done.returncode == 0, (done.stdout[-2000:], done.stderr[-3000:])
    return _last_json(done)


@pytest.fixture(scope="module")
def boards(kicad, tmp_path_factory) -> dict:
    """name -> pcb path; every board in its own directory with a minimal SES."""
    root = tmp_path_factory.mktemp("outline")
    pcbs = {}
    for name in BOARDS:
        (root / name).mkdir()
        pcbs[name] = root / name / "board.kicad_pcb"
        (root / name / "board.ses").write_text(MIN_SES, encoding="utf-8")
    spec_file = root / "specs.json"
    spec_file.write_text(json.dumps({str(pcbs[n]): s for n, s in BOARDS.items()}),
                         encoding="utf-8")
    _child(kicad, root, BUILD, spec_file, json.dumps({"t10": T10_BOARD, "neck": NECK_BOARD}))
    return pcbs


def _dump(kicad: env.KiCadInstall, pcb: Path) -> dict:
    return _child(kicad, pcb.parent, DUMP, pcb)[str(pcb)]


def _import_ses(kicad: env.KiCadInstall, pcb: Path, ses: Path | None = None) -> tuple:
    done = _run([kicad.python, TOOLS / "swig_import_ses.py", pcb, ses or pcb.with_suffix(".ses")])
    return done.returncode, _last_json(done)


def _drc(kicad: env.KiCadInstall, pcb: Path, tag: str) -> tuple[Path, dict]:
    rpt = pcb.with_name(f"{pcb.stem}_{tag}_drc.json")
    done = _run([kicad.kicad_cli, "pcb", "drc", pcb, "--severity-error", "--format", "json",
                 "-o", rpt])
    errors = [ln for ln in (done.stdout + done.stderr).splitlines()
              if ln.lstrip().startswith("Error:") or " Error:" in ln]
    assert rpt.is_file() and not errors, (done.stdout[-1500:], done.stderr[-1500:])
    return rpt, json.loads(rpt.read_text(encoding="utf-8"))


def _board_fixes(kicad: env.KiCadInstall, pcb: Path, drc: Path, mode: str,
                 design: dict | None = None) -> dict:
    netlist = pcb.with_name("netlist.json")
    netlist.write_text(json.dumps(design or {}), encoding="utf-8")
    done = _run([kicad.python, TOOLS / "swig_board_fixes.py", pcb, netlist, drc, mode])
    assert done.returncode == 0, (done.stdout[-2000:], done.stderr[-3000:])
    return _last_json(done)


def _board_frame(rec: dict) -> tuple[ol.Outline, tuple]:
    return ol.from_board_block(rec["outline"]), tuple(rec["origin"])


def _ring_items(ring_nm: list, origin: tuple) -> list:
    pts = [(x / 1e6 - origin[0], y / 1e6 - origin[1]) for x, y in ring_nm]
    return [{"kind": "track", "net": "GND", "start": a, "end": b, "width": 1e-6}   # 1 nm
            for a, b in zip(pts, pts[1:] + pts[:1], strict=True)]


def _track_items(tracks: list, origin: tuple) -> list:
    return [{"kind": "track", "net": net, "width": w,
             "start": (a[0] - origin[0], a[1] - origin[1]),
             "end": (b[0] - origin[0], b[1] - origin[1])} for net, _l, w, (a, b) in tracks]


def _in_hole(tracks: list, origin: tuple) -> list:
    """Added tracks whose copper (a disc of w/2 swept along) reaches into the hole."""
    hits = []
    for net, _l, w, (a, b) in tracks:
        for k in range(201):
            s = (a[0] + (b[0] - a[0]) * k / 200 - origin[0],
                 a[1] + (b[1] - a[1]) * k / 200 - origin[1])
            if math.dist(s, HOLE_C) < HOLE_R + w / 2:
                hits.append((net, s))
                break
    return hits


def _added(before: dict, after: dict) -> list:
    rest, added = list(before["tracks"]), []
    for t in after["tracks"]:
        if t in rest:
            rest.remove(t)
        else:
            added.append(t)
    return added


# --- (a) / (c): the SES-import GND pour -------------------------------------
def test_live_a_t10_pour_follows_the_outline_with_both_holes(kicad, boards):
    pcb = boards["pour_t10"]
    rc, res = _import_ses(kicad, pcb)
    assert rc == 0 and res["ok"] and res["warnings"] == [], res
    rec = _dump(kicad, pcb)
    outline, origin = _board_frame(rec)
    zones = rec["zones"]
    assert [(z["layer"], z["net"]) for z in zones] == [("B.Cu", "GND"), ("F.Cu", "GND")]
    for z in zones:
        assert z["outlines"] == 1 and z["holes"] == [2], z["holes"]
        assert z["area"] == pytest.approx(rec["plane_area"], rel=1e-9)
        items = [it for ring in z["rings"] for it in _ring_items(ring, origin)]
        assert oa.copper_issues(outline, items, POUR_INSET - 0.01) == []
        gap = min(ol.distance_to_edge(outline, it["start"]) for it in items)
        assert gap == pytest.approx(POUR_INSET, abs=0.01)
        assert z["filled_area"] > 1000 and z["filled_islands"] == 1
        assert z["filled_holes"] == 2, z["filled_holes"]
        filled = [it for ring in z["filled_rings"] for it in _ring_items(ring, origin)]
        assert oa.copper_issues(outline, filled, rec["edge_clearance"] - 0.01) == []


def test_live_a_narrow_neck_pours_every_piece(kicad, boards):
    pcb = boards["pour_neck"]
    rc, res = _import_ses(kicad, pcb)
    assert rc == 0 and res["ok"] and res["warnings"] == [], res
    rec = _dump(kicad, pcb)
    outline, origin = _board_frame(rec)
    # a zone keeps only its first outline through SaveBoard: one zone per lobe
    assert [(z["layer"], z["outlines"], z["holes"]) for z in rec["zones"]] == \
        [("B.Cu", 1, [0])] * 2 + [("F.Cu", 1, [0])] * 2
    for layer in ("B.Cu", "F.Cu"):
        zones = [z for z in rec["zones"] if z["layer"] == layer]
        assert sum(z["area"] for z in zones) == pytest.approx(rec["plane_area"], rel=1e-9)
        assert rec["plane_area"] == pytest.approx(2 * 19.4 * 29.4, abs=0.05)  # lobes, inset 0.3
        items = [it for z in zones for ring in z["rings"] for it in _ring_items(ring, origin)]
        assert oa.copper_issues(outline, items, POUR_INSET - 0.01) == []


def test_live_c_rect_board_pour_is_heads_bbox_rectangle(kicad, boards):
    pcb = boards["pour_rect"]
    rc, res = _import_ses(kicad, pcb)
    assert rc == 0 and res["ok"] and res["warnings"] == [], res
    rec = _dump(kicad, pcb)
    assert len(rec["zones"]) == 2
    for z in rec["zones"]:
        assert z["rings"] == [rec["legacy_rect"]] and z["holes"] == [0]


def test_live_open_outline_falls_back_to_the_bbox_with_a_warning(kicad, boards):
    pcb = boards["pour_open"]
    rc, res = _import_ses(kicad, pcb)
    assert rc == 0 and res["ok"], res
    pour = [w for w in res["warnings"] if "GND pour" in w]
    assert len(pour) == 2 and all("Edge.Cuts bounding box" in w for w in pour), res
    rec = _dump(kicad, pcb)
    assert rec["outline"] is None
    assert [z["rings"] for z in rec["zones"]] == [[rec["legacy_rect"]]] * 2


def test_live_import_ses_refuses_a_missing_or_unreadable_board(kicad, tmp_path):
    ses = tmp_path / "board.ses"
    ses.write_text(MIN_SES, encoding="utf-8")
    rc, res = _import_ses(kicad, tmp_path / "missing.kicad_pcb", ses)
    assert rc == 1 and res["ok"] is False and "board file not found" in res["error"], res
    garbage = tmp_path / "garbage.kicad_pcb"
    garbage.write_text("this is not a board\n", encoding="utf-8")
    rc, res = _import_ses(kicad, garbage, ses)
    assert rc == 1 and res["ok"] is False and "garbage.kicad_pcb" in res["error"], res


# --- (b): the healer and the via adders keep copper on the board -----------
def _heal(kicad, pcb: Path) -> tuple:
    """Pre-DRC, then heal a copy WITHOUT the guard (control) and the board itself
    through swig_board_fixes main. Returns (result, added, post DRC, control
    result, control added, origin, outline)."""
    rpt, _pre = _drc(kicad, pcb, "pre")
    before = _dump(kicad, pcb)
    control = pcb.with_name("control.kicad_pcb")
    shutil.copy(pcb, control)
    shutil.copy(pcb.with_suffix(".kicad_pro"), control.with_suffix(".kicad_pro"))
    ctl = _child(kicad, pcb.parent, NO_GUARD, control, rpt, "heal")
    result = _board_fixes(kicad, pcb, rpt, "heal")
    after = _dump(kicad, pcb)
    _post_rpt, post = _drc(kicad, pcb, "post")
    outline, origin = _board_frame(before)
    return (result, _added(before, after), post, ctl, _added(before, _dump(kicad, control)),
            origin, outline)


def test_live_b_heal_routes_round_the_hole_never_through_it(kicad, boards):
    result, added, post, ctl, ctl_added, origin, outline = _heal(kicad, boards["heal_around"])
    # control (no guard, HEAD's healer): the straight link runs through the hole
    assert ctl == {"applied": ["VIN: linked disconnected clusters (14.00 mm apart) on F.Cu "
                               "at 0.300 mm"], "open": []}
    assert _in_hole(ctl_added, origin)
    assert result == {"applied": ["VIN: linked disconnected clusters (14.00 mm apart) on F.Cu "
                                  "via maze route at 0.300 mm"], "open": []}, result
    assert added and _in_hole(added, origin) == []
    assert oa.copper_issues(outline, _track_items(added, origin), 0.5) == []
    assert post["unconnected_items"] == [] and post["violations"] == [], post["violations"]


def test_live_b_heal_refuses_when_only_the_hole_is_left(kicad, boards):
    result, added, post, ctl, ctl_added, origin, _o = _heal(kicad, boards["heal_refuse"])
    assert len(ctl["applied"]) == 1 and _in_hole(ctl_added, origin)      # control
    assert result == {"applied": [], "open": ["VIN: no clear path at the 0.300 mm default "
                                              "width"]}, result
    assert added == []
    assert len(post["unconnected_items"]) == 1
    assert not [v for v in post["violations"] if v.get("type") in EDGE_TYPES]


def test_live_parallel_vias_stay_out_of_the_holes_edge_clearance(kicad, boards):
    pcb = boards["vias_hole"]
    before = _dump(kicad, pcb)
    outline, origin = _board_frame(before)
    control = pcb.with_name("control.kicad_pcb")
    shutil.copy(pcb, control)
    ctl = _child(kicad, pcb.parent, NO_GUARD, control, "-", "vias")
    ctl_dump = _dump(kicad, control)
    ctl_vias = [v for v in ctl_dump["vias"] if v not in before["vias"]]
    assert ctl == {"applied": ["PWR: added parallel via (0.30 mm drill, 0.898 A IPC-2221) 1.2 mm "
                               "up the feed track — tied to the transition on B.Cu"],
                   "open": ["PWR: still 1 via(s) short — no safe spot on the feed tracks "
                            "(designer task)"]}
    assert ctl_vias == [["PWR", OX + 29.2, OY + 23.15]]                # unguarded: one via ...
    assert _added(before, ctl_dump) == [["PWR", "B.Cu", 0.2, [[OX + 29.2, OY + 23.15],
                                                              [OX + 28.0, OY + 23.15]]]]
    via = {"kind": "via", "net": "PWR", "center": (ctl_vias[0][1] - origin[0],
                                                     ctl_vias[0][2] - origin[1]), "radius": 0.3}
    assert oa.copper_issues(outline, [via], 0.5)                     # ... inside the clearance
    drc = pcb.with_name("empty_drc.json")
    drc.write_text("{}", encoding="utf-8")
    result = _board_fixes(kicad, pcb, drc, "vias", {"net_specs": {"PWR": {"current": 2.0}}})
    assert result == {"applied": [], "open": [
        "PWR: still 2 via(s) short — no safe spot on the feed tracks (designer task)"]}, result
    assert _dump(kicad, pcb)["vias"] == before["vias"]


def test_live_board_fixes_warns_when_the_outline_falls_back_to_the_bbox(kicad, boards):
    pcb = boards["pour_open"]
    drc = pcb.with_name("empty_drc.json")
    drc.write_text("{}", encoding="utf-8")
    result = _board_fixes(kicad, pcb, drc, "heal")
    assert result["applied"] == [] and result["open"] == []
    assert len(result["warnings"]) == 1 and "Edge.Cuts bounding box" in result["warnings"][0]
