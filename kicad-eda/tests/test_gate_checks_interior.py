"""swig_gate_checks — the G3 connector-at-edge rule honours ``"interior": true``.

A J* part the netlist flags interior (a config jumper, a Kelvin header, a
pin-socket wall) does not mate through the board edge; place_engine already
packs it as an ordinary part, so the G3 mouth / edge-distance check must not
report it either (docs/PIPELINE.md, the "interior" component key).

Unit tier: the pure helpers, and ``check_placement`` itself on a fake board.
swig_gate_checks imports pcbnew at module level; when pcbnew is absent (the dev
venv, CI) a stub carrying only ToMM and the courtyard layer ids is registered
for the import ONLY and removed again afterwards (as in test_place_engine).

Live tier: a real board built and saved by KiCad's bundled Python, loaded
again and checked with ``check_placement`` under the real pcbnew; skipped
without a KiCad Python that imports pcbnew.
"""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

import toolchain as tc

TOOLS = Path(__file__).resolve().parents[1] / "server" / "tools"
NM_PER_MM = 1_000_000


def _import_gate_checks() -> types.ModuleType:
    """Import swig_gate_checks; when pcbnew is absent, stub it for this import only."""
    try:
        import pcbnew  # noqa: F401  (KiCad's bundled python only)
    except ImportError:
        pass
    else:
        return importlib.import_module("swig_gate_checks")
    stub = types.ModuleType("pcbnew")
    stub.ToMM = lambda nm: nm / NM_PER_MM
    stub.F_CrtYd, stub.B_CrtYd, stub.F_Cu, stub.B_Cu = 46, 47, 0, 2
    sys.modules["pcbnew"] = stub
    try:
        return importlib.import_module("swig_gate_checks")
    finally:
        if sys.modules.get("pcbnew") is stub:
            del sys.modules["pcbnew"]


gc = _import_gate_checks()


def test_pcbnew_stub_does_not_leak_into_sys_modules():
    cached = sys.modules.get("pcbnew")
    assert cached is None or getattr(cached, "__file__", None), (
        "pcbnew stub leaked into sys.modules")


# --- pure helpers ------------------------------------------------------------

def test_interior_refs_reads_the_flag():
    design = {"components": [{"ref": "J1"}, {"ref": "J2", "interior": True},
                             {"ref": "J3", "interior": False}, {"ref": "U1", "interior": True}]}
    assert gc.interior_refs(design) == {"J2", "U1"}
    assert gc.interior_refs({}) == set()
    assert gc.interior_refs({"components": []}) == set()


@pytest.mark.parametrize("ref,interior,expected", [
    ("J1", set(), True),                  # an ordinary connector
    ("J1", {"J1"}, False),                # flagged interior: not an edge connector
    ("J2", {"J1"}, True),                 # the flag is per part
    ("JP1", set(), True),                 # any J* reference, as before
    ("U1", set(), False),
    ("U1", {"U1"}, False),
    ("", set(), False),
])
def test_is_edge_connector(ref, interior, expected):
    assert gc.is_edge_connector(ref, interior) is expected


# --- check_placement on a fake board (40 x 30 mm, origin top-left) -----------

@dataclass(frozen=True)
class _Vec:
    x: int
    y: int


@dataclass(frozen=True)
class _Box:
    x0: float
    y0: float
    x1: float
    y1: float

    def GetLeft(self) -> int:
        return round(self.x0 * NM_PER_MM)

    def GetTop(self) -> int:
        return round(self.y0 * NM_PER_MM)

    def GetRight(self) -> int:
        return round(self.x1 * NM_PER_MM)

    def GetBottom(self) -> int:
        return round(self.y1 * NM_PER_MM)

    def GetCenter(self) -> _Vec:
        return _Vec(round((self.x0 + self.x1) / 2 * NM_PER_MM),
                    round((self.y0 + self.y1) / 2 * NM_PER_MM))


@dataclass(frozen=True)
class _Courtyard:
    box: _Box

    def OutlineCount(self) -> int:
        return 1

    def BBox(self) -> _Box:
        return self.box


@dataclass(frozen=True)
class _Pad:
    x: float
    y: float

    def GetPosition(self) -> _Vec:
        return _Vec(round(self.x * NM_PER_MM), round(self.y * NM_PER_MM))


@dataclass(frozen=True)
class _Footprint:
    ref: str
    pads: tuple
    courtyard: _Box

    def GetReference(self) -> str:
        return self.ref

    def Pads(self) -> list:
        return list(self.pads)

    def GetCourtyard(self, _layer: int) -> _Courtyard:
        return _Courtyard(self.courtyard)


@dataclass(frozen=True)
class _Board:
    footprints: tuple
    edges: _Box = _Box(0.0, 0.0, 40.0, 30.0)

    def GetBoardEdgesBoundingBox(self) -> _Box:
        return self.edges

    def GetFootprints(self) -> list:
        return list(self.footprints)

    def Zones(self) -> list:
        return []


def _inward(ref: str, x: float = 20.0) -> _Footprint:
    """Near the top edge with its mouth (courtyard side) pointing DOWN, into the board."""
    return _Footprint(ref, (_Pad(x - 1, 4.0), _Pad(x + 1, 4.0)), _Box(x - 3, 3.0, x + 3, 9.0))


def _recessed(ref: str) -> _Footprint:
    """Mouth pointing UP at the top edge, but its courtyard 6 mm short of it."""
    return _Footprint(ref, (_Pad(19.0, 12.0), _Pad(21.0, 12.0)), _Box(17.0, 6.0, 23.0, 13.0))


def _design(*refs: str, interior: tuple = ()) -> dict:
    return {"components": [{"ref": r, **({"interior": True} if r in interior else {})}
                           for r in refs]}


def test_inward_connector_is_flagged_unless_interior():
    board = _Board((_inward("J1"),))
    issues, _ = gc.check_placement(board, _design("J1"))
    assert issues == ["J1: mating face points INTO the board interior "
                      "(unusable in an enclosure)"]
    issues, warnings = gc.check_placement(board, _design("J1", interior=("J1",)))
    assert issues == [] and warnings == []


def test_recessed_connector_is_flagged_unless_interior():
    board = _Board((_recessed("J1"),))
    issues, _ = gc.check_placement(board, _design("J1"))
    assert issues == ["J1: mating face is 6.0 mm from the board edge it faces "
                      "(must be at the edge, <=3 mm)"]
    issues, _ = gc.check_placement(board, _design("J1", interior=("J1",)))
    assert issues == []


def test_interior_flag_exempts_only_the_flagged_part():
    board = _Board((_inward("J1", x=10.0), _inward("J2", x=30.0)))
    issues, _ = gc.check_placement(board, _design("J1", "J2", interior=("J1",)))
    assert issues == ["J2: mating face points INTO the board interior "
                      "(unusable in an enclosure)"]


def test_check_placement_without_a_netlist_still_checks_every_connector():
    # main() hands check_placement {} when the netlist is missing or unreadable
    issues, _ = gc.check_placement(_Board((_inward("J1"),)), {})
    assert issues and issues[0].startswith("J1: mating face points INTO")


# --- live: a real board under KiCad's bundled Python -------------------------

# right-angle header: its courtyard (the pins' mating side) extends to +X of the
# pad column, so at (10, 10) rot 0 on a 40 x 30 board the mouth faces the centre
HEADER_LIB = "Connector_PinHeader_2.54mm.pretty"
HEADER = "PinHeader_1x04_P2.54mm_Horizontal"

BUILD_BOARD = """\
import sys
sys.path.insert(0, sys.argv[1])
import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew

out, fp_dir, fp_name = sys.argv[2], sys.argv[3], sys.argv[4]
mm = pcbnew.FromMM
board = pcbnew.BOARD()
corners = [(0, 0), (40, 0), (40, 30), (0, 30)]
for (x0, y0), (x1, y1) in zip(corners, corners[1:] + corners[:1]):
    seg = pcbnew.PCB_SHAPE(board)
    seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
    seg.SetLayer(pcbnew.Edge_Cuts)
    seg.SetStart(pcbnew.VECTOR2I(mm(x0), mm(y0)))
    seg.SetEnd(pcbnew.VECTOR2I(mm(x1), mm(y1)))
    board.Add(seg)
fp = pcbnew.FootprintLoad(fp_dir, fp_name)
if fp is None:
    raise SystemExit(f"FootprintLoad returned None for {fp_name} in {fp_dir}")
fp.SetReference("J1")
fp.SetPosition(pcbnew.VECTOR2I(mm(10), mm(10)))
board.Add(fp)
pcbnew.SaveBoard(out, board)
print("built", out)
"""


def _kicad_footprints(kpy: str) -> Path:
    """The footprint libraries of the SAME install as the Python (a newer
    install's footprints may be in a format this pcbnew cannot read)."""
    root = Path(kpy).resolve().parent.parent
    for share in (root / "share" / "kicad" / "footprints", Path("/usr/share/kicad/footprints")):
        if (share / HEADER_LIB).is_dir():
            return share / HEADER_LIB
    pytest.skip(f"no {HEADER_LIB} beside {kpy}")


@pytest.fixture(scope="module")
def live_board(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, Path]:
    kpy = tc.kicad_python()
    if not kpy:
        pytest.skip(f"no KiCad pcbnew Python: {tc.kicad_python_error()}")
    work = tmp_path_factory.mktemp("gate_interior")
    script = work / "build_board.py"
    script.write_text(BUILD_BOARD, encoding="utf-8")
    pcb = work / "interior.kicad_pcb"
    proc = subprocess.run([kpy, str(script), str(TOOLS), str(pcb), str(_kicad_footprints(kpy)),
                           HEADER], capture_output=True, text=True, timeout=180,
                          stdin=subprocess.DEVNULL)
    assert proc.returncode == 0 and pcb.is_file(), proc.stdout + proc.stderr
    return kpy, pcb


# check_placement on the loaded board, once per design. KiCad 9.0.4's LoadBoard
# leaves every footprint's courtyard cache EMPTY (GetCourtyard() has no
# outline until BuildCourtyardCaches(); 10.0 builds it lazily), so the caches
# are built here first — otherwise no connector has a mouth on 9.0 at all.
CHECK_PLACEMENT = """\
import json
import sys
sys.path.insert(0, sys.argv[1])
import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew
import swig_gate_checks

board = pcbnew.LoadBoard(sys.argv[2])
for fp in board.GetFootprints():
    fp.BuildCourtyardCaches()
results = [swig_gate_checks.check_placement(board, design)[0]
           for design in json.loads(sys.argv[3])]
print(json.dumps(results))
"""


def _run_check_placement(kpy: str, pcb: Path, designs: list, work: Path) -> list:
    script = work / "check_placement.py"
    script.write_text(CHECK_PLACEMENT, encoding="utf-8")
    proc = subprocess.run([kpy, str(script), str(TOOLS), str(pcb), json.dumps(designs)],
                          capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("[")]
    assert proc.returncode == 0 and lines, proc.stdout + proc.stderr
    return json.loads(lines[-1])


def test_live_interior_header_is_not_an_edge_connector(live_board, tmp_path: Path):
    kpy, pcb = live_board
    flagged, exempt = _run_check_placement(
        kpy, pcb, [_design("J1"), _design("J1", interior=("J1",))], tmp_path)
    assert flagged == ["J1: mating face points INTO the board interior "
                       "(unusable in an enclosure)"]
    assert exempt == []
