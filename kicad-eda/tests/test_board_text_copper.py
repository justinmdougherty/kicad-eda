"""board_text_copper (F3-20): tracks, arcs and vias read from the text of any
.kicad_pcb version, and the G4 copper-containment check rp_feature_outline's
g4_inputs hook runs on a board pcbnew cannot load (a resumed KiCad 11 board).

The KiCad 11 fixture is built here: the copper items are VERBATIM what KiCad
10.99 wrote when `kicad-cli pcb upgrade` re-saved a KiCad 9 board carrying the
T10 outline (60 x 40, r=4, a stadium slot and a 5 mm hole) at sheet offset
(100, 100), and the Edge.Cuts are emitted in the same KiCad 11 syntax from the
outline's exact primitives. Real KiCad 11 DRC on that board flags the track
across the slot and the arc past the edge (edge clearance) but NOT the via
inside the hole, which is why this check exists.
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

import board_text_copper as btc
import kicad_env as env
import kicad_server as k
import outline as ol
import outline_audit as oa
import rp_context
import rp_feature_outline as rfo
import toolchain as tc
from fixtures import pipeline_world
from fixtures.swig_python import PREAMBLE, T10_BOARD, run_child

world = pipeline_world.world            # the fixture (an assignment: see pipeline_world)

TOOLS = Path(__file__).resolve().parents[1] / "server" / "tools"
OX, OY = 100.0, 100.0                    # where the T10 board frame sits on the sheet

K11_HEAD = """(kicad_pcb
\t(version 20260901)
\t(generator "pcbnew")
\t(generator_version "10.99")
\t(general
\t\t(thickness 1.6)
\t\t(legacy_teardrops no)
\t)
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(25 "Edge.Cuts" user)
\t\t(31 "F.CrtYd" user "F.Courtyard")
\t)
\t(setup
\t\t(pad_to_mask_clearance 0)
\t\t(aux_axis_origin 110 130)
\t\t(grid_origin 110 130)
\t)
"""
# a KiCad 11 footprint (transform placement) inside the board: its pads are not tracks
K11_FOOTPRINT = """\t(footprint "Resistor_SMD:R_0603_1608Metric"
\t\t(layer "F.Cu")
\t\t(uuid "0a1d0001-0000-4000-8000-000000000001")
\t\t(transform
\t\t\t(translate 150 135)
\t\t\t(rotate 0)
\t\t\t(scale 1 1)
\t\t)
\t\t(property "Reference" "R1"
\t\t\t(at 0 -1.43 0)
\t\t\t(layer "F.SilkS")
\t\t\t(uuid "0a1d0001-0000-4000-8000-00000000000b")
\t\t)
\t\t(attr smd)
\t\t(pad "1" smd roundrect
\t\t\t(at -0.825 0)
\t\t\t(size 0.8 0.95)
\t\t\t(layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net "GND")
\t\t\t(uuid "0a1d0001-0000-4000-8000-000000000100")
\t\t)
\t\t(pad "2" smd roundrect
\t\t\t(at 0.825 0)
\t\t\t(size 0.8 0.95)
\t\t\t(layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net "Net-(R1-Pad2)")
\t\t\t(uuid "0a1d0001-0000-4000-8000-000000000101")
\t\t)
\t)
"""
_VIA = """\t(via
\t\t(at {at})
\t\t(size 0.6)
\t\t(drill 0.3)
\t\t(layers "F.Cu" "B.Cu")
\t\t(capping no)
\t\t(covering
\t\t\t(front no)
\t\t\t(back no)
\t\t)
\t\t(plugging
\t\t\t(front no)
\t\t\t(back no)
\t\t)
\t\t(filling no)
\t\t(net "GND")
\t\t(uuid "{uuid}")
\t)
"""
LEGIT = (_VIA.format(at="115 130", uuid="854614d9-3d1b-4889-a059-14f93494a18d") + """\t(segment
\t\t(start 105 125)
\t\t(end 120 125)
\t\t(width 0.4)
\t\t(layer "B.Cu")
\t\t(net "GND")
\t\t(uuid "bd727bee-355a-4cb9-9ff1-8f14b9d92a5e")
\t)
\t(segment
\t\t(start 105 115)
\t\t(end 120 115)
\t\t(width 0.25)
\t\t(layer "F.Cu")
\t\t(net "SIG")
\t\t(uuid "a8b5aa2e-aa90-4ec1-b407-cd24f4c53719")
\t)
\t(arc
\t\t(start 140 130)
\t\t(mid 142 128)
\t\t(end 144 130)
\t\t(width 0.25)
\t\t(layer "F.Cu")
\t\t(net "SIG")
\t\t(uuid "d6bac212-899f-4e09-8de0-92f2f97711a0")
\t)
""")
VIA_IN_HOLE = _VIA.format(at="130 120", uuid="bfb4367c-6cf2-4fd1-af16-1223f223eb3f")
TRACK_ACROSS_SLOT = """\t(segment
\t\t(start 124 109)
\t\t(end 136 109)
\t\t(width 0.25)
\t\t(layer "F.Cu")
\t\t(net "SIG")
\t\t(uuid "6102b476-409c-4358-a800-0d31ff0aac12")
\t)
"""
ARC_PAST_EDGE = """\t(arc
\t\t(start 101 120)
\t\t(mid 99 122)
\t\t(end 101 124)
\t\t(width 0.25)
\t\t(layer "B.Cu")
\t\t(net "SIG")
\t\t(uuid "42b232e9-db40-46a4-8743-0ffaa6c2674f")
\t)
"""
BAD = VIA_IN_HOLE + TRACK_ACROSS_SLOT + ARC_PAST_EDGE


def _n(v: float) -> str:
    """A coordinate the way KiCad writes it (no trailing zeros)."""
    return f"{v:.6f}".rstrip("0").rstrip(".")


def _xy(name: str, p: tuple, dx: float = OX, dy: float = OY) -> str:
    return f"\t\t({name} {_n(p[0] + dx)} {_n(p[1] + dy)})\n"


def k11_edges(block: dict = T10_BOARD, dx: float = OX, dy: float = OY) -> str:
    """The outline's exact primitives as KiCad 11 Edge.Cuts gr_* items."""
    out = []
    for i, p in enumerate(ol.to_kicad_primitives(ol.from_board_block(block))):
        if p["kind"] == "circle":
            cx, cy = p["center"]
            body = _xy("center", (cx, cy), dx, dy) + _xy("end", (cx + p["radius"], cy), dx, dy)
            head, fill = "gr_circle", "\t\t(fill no)\n"
        else:
            keys = ("start", "mid", "end") if p["kind"] == "arc" else ("start", "end")
            body = "".join(_xy(key, p[key], dx, dy) for key in keys)
            head, fill = ("gr_arc" if p["kind"] == "arc" else "gr_line"), ""
        out.append(f"\t({head}\n{body}\t\t(stroke\n\t\t\t(width 0.1)\n\t\t\t(type default)\n"
                   f"\t\t)\n{fill}\t\t(layer \"Edge.Cuts\")\n"
                   f"\t\t(uuid \"00000000-0000-4000-8000-{i:012d}\")\n\t)\n")
    return "".join(out)


def k11_board(items: str = LEGIT + BAD, edges: str | None = None) -> str:
    """A KiCad 11 board: the T10 outline at (100, 100), a footprint, `items`."""
    edge_text = k11_edges() if edges is None else edges
    return K11_HEAD + edge_text + K11_FOOTPRINT + items + "\t(embedded_fonts no)\n)\n"


def t10_design() -> dict:
    return {"title": "T10 copper check", "board": copy.deepcopy(T10_BOARD),
            "components": copy.deepcopy(pipeline_world.DESIGN["components"]),
            "nets": copy.deepcopy(pipeline_world.DESIGN["nets"])}


def _by_key(items: tuple) -> dict:
    return {(it["kind"], tuple(it.get("start") or it["center"])): it for it in items}


# --- reading ---------------------------------------------------------------------------

def test_reads_the_items_kicad_11_wrote():
    board = btc.read_copper(k11_board())
    assert board.format_version == 20260901
    assert sorted(it["kind"] for it in board.items) == \
        ["arc", "arc", "track", "track", "track", "via", "via"]
    got = _by_key(board.items)
    assert got[("via", (130.0, 120.0))] == {
        "kind": "via", "layers": ("F.Cu", "B.Cu"), "net": "GND", "center": (130.0, 120.0),
        "radius": 0.3, "drill": 0.3, "via_type": "through"}
    assert got[("track", (124.0, 109.0))] == {
        "kind": "track", "layer": "F.Cu", "net": "SIG", "start": (124.0, 109.0),
        "end": (136.0, 109.0), "width": 0.25}
    assert got[("arc", (101.0, 120.0))] == {
        "kind": "arc", "layer": "B.Cu", "net": "SIG", "start": (101.0, 120.0),
        "mid": (99.0, 122.0), "end": (101.0, 124.0), "width": 0.25}


def test_footprint_pads_and_drawings_are_not_copper_items():
    board = btc.read_copper(k11_board(items=""))
    assert board.items == ()


def test_items_keep_the_file_order_and_are_frozen_tuples_of_dicts():
    board = btc.read_copper(k11_board())
    assert isinstance(board.items, tuple)
    assert [it["kind"] for it in board.items] == \
        ["via", "track", "track", "arc", "via", "track", "arc"]


K9 = """(kicad_pcb (version 20241229) (generator "pcbnew") (generator_version "9.0")
  (net 0 "")
  (net 1 "GND")
  (net 2 "/A{slash}B")
  (segment (start 1 2) (end 3 4) (width 0.25) (layer "F.Cu") (net 1) (uuid "a"))
  (segment (start 5 6) (end 7 8) (width 0.2) (layer "B.Cu") (net 0) (uuid "b"))
  (arc (start 0 0) (mid 1 1) (end 2 0) (width 0.3) (layer "In1.Cu") (net 2) (uuid "c"))
  (via (at 9 9) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1) (uuid "d"))
)
"""


def test_kicad9_net_codes_resolve_through_the_net_table():
    items = btc.read_copper(K9).items
    assert [it["net"] for it in items] == ["GND", "", "/A/B", "GND"]
    assert items[2]["layer"] == "In1.Cu" and items[2]["mid"] == (1.0, 1.0)
    assert btc.read_copper(K9).format_version == 20241229


K5 = """(kicad_pcb (version 20171130) (host pcbnew 5.1.6)
  (net 0 "")
  (net 1 GND)
  (net 2 01)
  (segment (start 1 2) (end 3 4) (width 0.25) (layer F.Cu) (net 2) (tstamp 5A0))
  (via blind (at 5 6) (size 0.8) (drill 0.4) (layers F.Cu In1.Cu) (net 1))
  (via micro locked (at 7 7) (size 0.3) (drill 0.1) (layers F.Cu In1.Cu) (net 1))
)
"""


def test_kicad5_bare_tokens_keep_their_text():
    items = btc.read_copper(K5).items
    assert items[0] == {"kind": "track", "layer": "F.Cu", "net": "01", "start": (1.0, 2.0),
                        "end": (3.0, 4.0), "width": 0.25}
    assert (items[1]["via_type"], items[1]["layers"], items[1]["net"]) == \
        ("blind", ("F.Cu", "In1.Cu"), "GND")
    assert items[2]["via_type"] == "micro" and items[2]["radius"] == pytest.approx(0.15)


def test_a_padstack_via_uses_its_largest_copper_size():
    text = ('(kicad_pcb (version 20260206) (generator "pcbnew") (generator_version "10.0")\n'
            '  (via (at 1 1) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")\n'
            '    (padstack (mode front_inner_back) (layer "Inner" (size 0.45))\n'
            '      (layer "B.Cu" (size 0.8)))\n'
            '    (net "GND") (uuid "e"))\n)\n')
    (via,) = btc.read_copper(text).items
    assert via["radius"] == pytest.approx(0.4)


def test_quoted_names_keep_their_parens_and_escapes():
    text = ('(kicad_pcb (version 20260901) (generator "pcbnew") (generator_version "10.99")\n'
            '  (segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") '
            '(net "Net-(R1-Pad2)") (uuid "f"))\n'
            '  (segment (start 0 1) (end 1 1) (width 0.2) (layer "F.Cu") '
            '(net "A\\"B (x)") (uuid "g"))\n'
            '  (segment (start 0 2) (end 1 2) (width 0.2) (layer "F.Cu") (uuid "h"))\n)\n')
    assert [it["net"] for it in btc.read_copper(text).items] == \
        ["Net-(R1-Pad2)", 'A"B (x)', ""]


def test_a_teardrop_block_without_its_paren_is_read_as_kicad_reads_it():
    text = ('(kicad_pcb (version 20241229) (generator "pcbnew") (generator_version "9.0")\n'
            '  (net 1 "GND")\n'
            '  (via (at 2 3) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")\n'
            '    (teardrops (best_length_ratio 0.5) (curved_edges no)filter_ratio 0.9)\n'
            '      (enabled no)) (net 1) (uuid "t"))\n)\n')
    (via,) = btc.read_copper(text).items
    assert via["center"] == (2.0, 3.0) and via["net"] == "GND"


@pytest.mark.parametrize("text, message", [
    ("(footprint \"x\" (layer \"F.Cu\"))", "not a KiCad board"),
    ('(kicad_pcb (version 20260901) (segment (start 0 0) (end 1 1) (width 0.2)', "unbalanced"),
    ('(kicad_pcb (version 20260901)))', "closes nothing"),
    ('(kicad_pcb (version 20260901)) (via (at 1 1) (size 1))', "after the board's closing"),
    ('(kicad_pcb (version 20260901) (segment (end 1 1) (width 0.2) (layer "F.Cu")))',
     "segment.*start"),
    ('(kicad_pcb (version 20260901) (via (at 1 1) (drill 0.3) (layers "F.Cu" "B.Cu")))',
     "via.*size"),
    ('(kicad_pcb (version 20241229) (net 1 "A") (segment (start 0 0) (end 1 1) (width 0.2) '
     '(layer "F.Cu") (net 7)))', "net 7"),
    ('(kicad_pcb (version 20260901) (segment (start 0 x) (end 1 1) (width 0.2)))',
     "segment.*'x'"),
], ids=["not-a-board", "truncated", "extra-paren", "trailing-item", "no-start", "no-size",
        "unknown-net-code", "not-a-number"])
def test_malformed_boards_raise_value_error_naming_the_item(text, message):
    with pytest.raises(ValueError, match=message):
        btc.read_copper(text)


def test_read_copper_file_reads_any_path(tmp_path):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(k11_board(), encoding="utf-8")
    assert btc.read_copper_file(pcb) == btc.read_copper(k11_board())
    with pytest.raises(OSError):
        btc.read_copper_file(tmp_path / "missing.kicad_pcb")


def test_translated_returns_new_records_and_leaves_the_input_alone():
    items = btc.read_copper(k11_board()).items
    before = copy.deepcopy(items)
    moved = btc.translated(items, -OX, -OY)
    assert items == before
    got = _by_key(tuple(moved))
    assert got[("via", (30.0, 20.0))]["radius"] == 0.3
    assert got[("arc", (1.0, 20.0))]["mid"] == (-1.0, 22.0)
    assert got[("track", (24.0, 9.0))]["end"] == (36.0, 9.0)


def test_runs_without_site_packages():
    """Stdlib + sexpr_read only: a -I -S interpreter (no sexpdata) reads the
    board and loads no third-party module."""
    code = ("import json, sys\n"
            f"sys.path.insert(0, {str(TOOLS)!r})\n"
            "import board_text_copper as btc\n"
            "n = len(btc.read_copper(sys.stdin.read()).items)\n"
            "std = sys.stdlib_module_names\n"
            "extra = sorted(m for m in sys.modules if m.split('.')[0] not in std\n"
            "               and m not in ('__main__', 'board_text_copper', 'sexpr_read'))\n"
            "print(json.dumps({'items': n, 'extra': extra}))\n")
    proc = subprocess.run([sys.executable, "-I", "-S", "-c", code], input=k11_board(),
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"items": 7, "extra": []}


# --- the containment check on the board text -------------------------------------------

def test_outline_audit_finds_exactly_the_copper_off_the_material():
    items = btc.translated(btc.read_copper(k11_board()).items, -OX, -OY)
    issues = oa.copper_issues(ol.from_board_block(T10_BOARD), items, 0.0)
    assert [(i.code, i.ref) for i in issues] == \
        [("copper_outside", "GND"), ("copper_outside", "SIG"), ("copper_outside", "SIG")]
    assert "into cutout 2" in issues[0].message and "via" in issues[0].message
    assert "into cutout 1" in issues[1].message
    assert "past the board outline" in issues[2].message


@pytest.fixture
def k11_ctx(tmp_path, monkeypatch):
    """A RunContext on a KiCad 11 T10 board pcbnew cannot load."""
    monkeypatch.setattr(tc, "swig_readable", lambda pcb: f"{Path(pcb).name} is KiCad 11")
    ctx = rp_context.RunContext(netlist=str(tmp_path / "board.json"), w=60, h=40)
    ctx.design = t10_design()
    Path(ctx.pcb).write_text(k11_board(), encoding="utf-8")
    return ctx


def test_g4_inputs_turns_copper_in_a_cutout_into_g4_blockers(k11_ctx):
    drc = {"errors": 0, "unconnected": 0, "types": {}}
    routed = {"issues": ["an existing issue"], "warnings": ["w"]}
    raw = copy.deepcopy((drc, routed))
    out_drc, out_routed = rfo.g4_inputs(k11_ctx, drc, routed)
    assert (drc, routed) == raw                         # the raw measurements stay raw
    assert out_drc == drc and out_routed["warnings"] == ["w"]
    issues = out_routed["issues"]
    assert issues[0] == "an existing issue" and len(issues) == 4
    assert all(i.startswith("copper_outside: ") for i in issues[1:])
    assert "via on net 'GND'" in issues[1] and "into cutout 2" in issues[1]
    assert all("board.kicad_pcb" in i for i in issues[1:])


def test_g4_inputs_leaves_a_clean_kicad11_board_alone(k11_ctx):
    Path(k11_ctx.pcb).write_text(k11_board(items=LEGIT), encoding="utf-8")
    assert rfo.g4_inputs(k11_ctx, {"errors": 0}, {"issues": [], "warnings": []}) is None


def test_g4_inputs_leaves_a_swig_readable_board_to_the_swig_gate_check(k11_ctx, monkeypatch):
    monkeypatch.setattr(tc, "swig_readable", lambda pcb: None)
    assert rfo.g4_inputs(k11_ctx, {"errors": 0}, {"issues": [], "warnings": []}) is None


@pytest.mark.parametrize("edges, why", [
    ("", "Edge.Cuts"),
    (k11_edges({"outline": {"loop": [[0, 0], [70, 0], [70, 40], [0, 40]]}}), "70.000 x 40.000"),
], ids=["no-edge-cuts", "edge-cuts-differ-from-the-netlist"])
def test_g4_inputs_says_why_it_cannot_judge(k11_ctx, edges, why):
    Path(k11_ctx.pcb).write_text(k11_board(edges=edges), encoding="utf-8")
    _, routed = rfo.g4_inputs(k11_ctx, {"errors": 0}, {"issues": [], "warnings": []})
    (blocker,) = routed["issues"]
    assert blocker.startswith("copper containment not judged") and why in blocker
    assert "board.kicad_pcb" in blocker


def test_g4_inputs_never_crashes_the_run(k11_ctx, monkeypatch):
    def broken(text: str) -> btc.BoardCopper:
        raise RuntimeError("reader bug")

    monkeypatch.setattr(btc, "read_copper", broken)
    _, routed = rfo.g4_inputs(k11_ctx, {"errors": 0}, {"issues": [], "warnings": []})
    assert routed["issues"] == [f"{rfo.NOT_JUDGED} on board.kicad_pcb, which pcbnew cannot "
                                "load: RuntimeError: reader bug"]
    Path(k11_ctx.pcb).unlink()
    _, routed = rfo.g4_inputs(k11_ctx, {"errors": 0}, {"issues": [], "warnings": []})
    assert routed["issues"][0].startswith(f"{rfo.NOT_JUDGED} on board.kicad_pcb")


def test_g4_inputs_without_a_netlist_outline_judges_the_board_edge_cuts(k11_ctx):
    k11_ctx.design = {k: v for k, v in k11_ctx.design.items() if k != "board"}
    _, routed = rfo.g4_inputs(k11_ctx, {"errors": 0}, {"issues": [], "warnings": []})
    assert len(routed["issues"]) == 3


def test_g4_inputs_caps_the_blocker_list(k11_ctx):
    vias = "".join(_VIA.format(at=f"{129 + 0.1 * i:.1f} 120", uuid=f"u{i}") for i in range(30))
    Path(k11_ctx.pcb).write_text(k11_board(items=vias), encoding="utf-8")
    _, routed = rfo.g4_inputs(k11_ctx, {"errors": 0}, {"issues": [], "warnings": []})
    assert len(routed["issues"]) == rfo.MAX_COPPER_BLOCKERS + 1
    assert routed["issues"][-1].startswith("copper_outside: ") and "10 more" in \
        routed["issues"][-1]


def test_g4_inputs_through_the_harness_fails_g4_on_a_kicad11_board(world, monkeypatch):
    """A scripted run whose router leaves a KiCad 11 board with a via in the
    5 mm hole: the copper_outside blocker reaches the G4 verdict through the
    g4_inputs hook (the fake DRC and routed check are clean)."""
    world.write_design(t10_design())
    world.use_features("outline")
    route = k.autoroute_board

    def route_then_save_as_kicad11(pcb: str, *args, **kw) -> dict:
        res = route(pcb, *args, **kw)
        Path(pcb).write_text(k11_board(items=LEGIT + VIA_IN_HOLE), encoding="utf-8")
        return res

    monkeypatch.setattr(k, "autoroute_board", route_then_save_as_kicad11)
    rep = world.run()
    g4 = next(g for g in rep["gates"] if g["gate"] == "G4")
    assert g4["passed"] is False
    assert [b for b in g4["blockers"] if b.startswith("copper_outside")] == [
        b for b in g4["blockers"]]
    assert len(g4["blockers"]) == 1 and "into cutout 2" in g4["blockers"][0]
    assert rep["drc"] == pipeline_world.CLEAN_DRC        # the raw DRC is kept as measured
    assert "G5" not in [g["gate"] for g in rep["gates"]]


# --- live: a board pcbnew saved, upgraded to KiCad 11 by the nightly's kicad-cli -------

DRAW_COPPER = PREAMBLE + """
import board_outline as bo
import outline as ol
block, out = json.loads(sys.argv[1]), sys.argv[2]
OX, OY = 100.0, 100.0
board = pcbnew.CreateEmptyBoard()
model = ol.from_board_block(block)
bo.draw_outline(board, model, OX, OY)
bo.set_datum(board, model.datum[0] + OX, model.datum[1] + OY)
nets = {}
for name in ("GND", "SIG"):
    nets[name] = pcbnew.NETINFO_ITEM(board, name)
    board.Add(nets[name])
v = lambda x, y: pcbnew.VECTOR2I(pcbnew.FromMM(x + OX), pcbnew.FromMM(y + OY))
for kind, pts, net in (("track", [(5, 15), (20, 15)], "SIG"), ("track", [(24, 9), (36, 9)], "SIG"),
                       ("arc", [(1, 20), (-1, 22), (1, 24)], "SIG"),
                       ("via", [(15, 30)], "GND"), ("via", [(30, 20)], "GND")):
    item = {"track": pcbnew.PCB_TRACK, "arc": pcbnew.PCB_ARC, "via": pcbnew.PCB_VIA}[kind](board)
    if kind == "via":
        item.SetPosition(v(*pts[0]))
        item.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        item.SetWidth(pcbnew.FromMM(0.6))
        item.SetDrill(pcbnew.FromMM(0.3))
    else:
        item.SetStart(v(*pts[0]))
        if kind == "arc":
            item.SetMid(v(*pts[1]))
        item.SetEnd(v(*pts[-1]))
        item.SetWidth(pcbnew.FromMM(0.25))
        item.SetLayer(pcbnew.F_Cu)
    item.SetNet(nets[net])
    board.Add(item)
pcbnew.SaveBoard(out, board)
print(json.dumps({"version": pcbnew.Version()}))
"""


def _nightly_cli() -> str:
    found = next((i for i in env.discover() if i.version_tuple >= (10, 99, 0)), None)
    if found is None:
        pytest.skip("no KiCad 11 nightly (kicad-cli 10.99+) installed")
    return str(found.kicad_cli)


def test_live_a_board_upgraded_to_kicad11_is_judged_from_its_text(tmp_path):
    cli = _nightly_cli()
    pcb = tmp_path / "board.kicad_pcb"
    run_child(DRAW_COPPER, [json.dumps(T10_BOARD), pcb], tmp_path)
    proc = subprocess.run([cli, "pcb", "upgrade", str(pcb)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                          timeout=180)
    assert proc.returncode == 0 and "Error:" not in proc.stdout + proc.stderr, proc
    assert tc.swig_readable(str(pcb)) is not None        # pcbnew 9/10 cannot load it now
    ctx = rp_context.RunContext(netlist=str(tmp_path / "board.json"), w=60, h=40)
    ctx.design = t10_design()
    assert sorted(it["kind"] for it in btc.read_copper_file(pcb).items) == \
        ["arc", "track", "track", "via", "via"]
    _, routed = rfo.g4_inputs(ctx, {"errors": 0}, {"issues": [], "warnings": []})
    issues = routed["issues"]
    assert len(issues) == 3 and all(i.startswith("copper_outside: ") for i in issues)
    assert sum("into cutout 2" in i and "via" in i for i in issues) == 1
    assert sum("into cutout 1" in i for i in issues) == 1
    assert sum("past the board outline" in i for i in issues) == 1
