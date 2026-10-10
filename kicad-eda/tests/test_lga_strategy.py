"""lga_strategy: F2-S1's LGA inner-pad strategies as netlist hints (F2-S1b, ROADMAP D1).

Unit tier (venv only):
  * the LG290P hints reproduce F2-S1's winning netlist (SCR wave2/F2-S1/winner/netlist.json):
    the same keys (preroutes only), the same 80 preroute entries and 40 via coordinates within
    0.001 mm, with U1 at (21, 16); from template pads and from fp_reader records alike;
  * determinism (any pad order, repeated calls), a 3x3 synthetic grid, skipped channels
    (tight pitch, fab floors, foreign copper), strategies (a)-(e) keys, gnd_net / gnd_pads;
  * place_hints (KiCad rotation), apply_hints (a NEW netlist, builder-default planes parity),
    the fab defaults equal swig_build_board.FAB_RULES_MM, stdlib-only imports, < 400 lines.
Live tier (skipped without KiCad): KiCad 9's Python 3.11 imports the module and gives the
same hints; the oldest pcbnew's pad positions under rotation equal place_point.
"""
from __future__ import annotations

import ast
import copy
import json
import random
import subprocess
import sys
from pathlib import Path

import pytest

import build_features
import fp_author as fa
import fp_author_templates as ft
import fp_reader
import kicad_env as env
import lga_strategy as lga
import netlist_schema

TOOLS = Path(lga.__file__).resolve().parent
LG290P_GND = [n for n, f in ft.LG290P_PAD_FUNCTIONS.items() if f == "GND"]
U1 = (21.0, 16.0)                     # F2-S1 winner: U1 x/y = footprint origin (centred courtyard)

# F2-S1 winner/netlist.json "preroutes", footprint frame (board minus U1): channel centre x y,
# then corner pads A B C D = (x0,y0) (x1,y0) (x1,y1) (x0,y1); entries A->c->C (+ via) and B->c->D.
WINNER_VIAS = """
-2.1 -6.3 25 36 37 26 | -0.7 -6.3 36 47 48 37 | 0.7 -6.3 47 58 59 48 | 2.1 -6.3 58 69 70 59
-2.1 -4.9 26 37 38 27 | -0.7 -4.9 37 48 49 38 | 0.7 -4.9 48 59 60 49 | 2.1 -4.9 59 70 71 60
-2.1 -3.5 27 38 39 28 | -0.7 -3.5 38 49 50 39 | 0.7 -3.5 49 60 61 50 | 2.1 -3.5 60 71 72 61
-2.1 -2.1 28 39 40 29 | -0.7 -2.1 39 50 51 40 | 0.7 -2.1 50 61 62 51 | 2.1 -2.1 61 72 73 62
-2.1 -0.7 29 40 41 30 | -0.7 -0.7 40 51 52 41 | 0.7 -0.7 51 62 63 52 | 2.1 -0.7 62 73 74 63
-2.1  0.7 30 41 42 31 | -0.7  0.7 41 52 53 42 | 0.7  0.7 52 63 64 53 | 2.1  0.7 63 74 75 64
-2.1  2.1 31 42 43 32 | -0.7  2.1 42 53 54 43 | 0.7  2.1 53 64 65 54 | 2.1  2.1 64 75 76 65
-2.1  3.5 32 43 44 33 | -0.7  3.5 43 54 55 44 | 0.7  3.5 54 65 66 55 | 2.1  3.5 65 76 77 66
-2.1  4.9 33 44 45 34 | -0.7  4.9 44 55 56 45 | 0.7  4.9 55 66 67 56 | 2.1  4.9 66 77 78 67
-2.1  6.3 34 45 46 35 | -0.7  6.3 45 56 57 46 | 0.7  6.3 56 67 68 57 | 2.1  6.3 67 78 79 68
"""


def winner_rows() -> list[tuple[float, float, str, str, str, str]]:
    cells = [c.split() for line in WINNER_VIAS.strip().splitlines() for c in line.split("|")]
    return [(float(x), float(y), a, b, c, d) for x, y, a, b, c, d in cells]


def winner_preroutes(ox: float = U1[0], oy: float = U1[1]) -> list[dict]:
    """F2-S1's winning preroutes (board frame, U1 at (ox, oy))."""
    out = []
    for x, y, a, b, c, d in winner_rows():
        vx, vy = ox + x, oy + y
        out.append({"net": "GND", "width": 0.25, "layer": "F.Cu",
                    "path": [["pad", "U1", a], ["xy", vx, vy], ["pad", "U1", c]],
                    "vias": [[vx, vy]], "via_size": [0.5, 0.25]})
        out.append({"net": "GND", "width": 0.25, "layer": "F.Cu",
                    "path": [["pad", "U1", b], ["xy", vx, vy], ["pad", "U1", d]]})
    return out


def assert_preroutes_close(got: list[dict], want: list[dict], tol: float = 1e-3) -> None:
    assert len(got) == len(want)
    for i, (g, w) in enumerate(zip(got, want, strict=True)):
        assert set(g) == set(w), i
        assert (g["net"], g["width"], g["layer"]) == (w["net"], w["width"], w["layer"]), i
        assert g.get("via_size") == w.get("via_size"), i
        assert len(g["path"]) == len(w["path"]), i
        for p, q in zip(g["path"], w["path"], strict=True):
            assert p[0] == q[0], i
            if p[0] == "pad":
                assert p == q, i
            else:
                assert p[1:] == pytest.approx(q[1:], abs=tol), i
        for v, u in zip(g.get("vias", []), w.get("vias", []), strict=True):
            assert v == pytest.approx(u, abs=tol), i


def grid(n: int, pitch: float = 1.4, size: float = 1.0, *, first: int = 1) -> list[dict]:
    """An n x n grid of square pads centred on the origin, numbered column-major."""
    c = (n - 1) * pitch / 2
    return [{"number": str(first + col * n + row), "x": round(col * pitch - c, 6),
             "y": round(row * pitch - c, 6), "w": size, "h": size, "shape": "rect",
             "layers": ["F.Cu", "F.Mask", "F.Paste"]}
            for col in range(n) for row in range(n)]


def lg290p_hints(**kw) -> dict:
    return lga.netlist_hints(ft.lg290p().pads, gnd_pads=LG290P_GND, **kw)


# ---------------------------------------------------------------------------
# The F2-S1 winner
# ---------------------------------------------------------------------------
def test_lg290p_hints_match_the_f2s1_winning_netlist():
    hints = lg290p_hints()
    assert set(hints) == {"preroutes", "notes"}       # the winner adds preroutes only
    placed = lga.place_hints(hints, origin=U1)
    assert_preroutes_close(placed["preroutes"], winner_preroutes())
    assert_preroutes_close(hints["preroutes"], winner_preroutes(0.0, 0.0))
    assert lga.WINNER == "f" and lga.STRATEGIES["f"] == "f_escape_vias"
    assert hints == lg290p_hints(strategy="f") == lg290p_hints(strategy="f_escape_vias")


def test_fp_reader_records_give_the_same_hints(tmp_path):
    path = fa.write_footprint(ft.lg290p(), tmp_path / "parts.pretty")
    parsed = fp_reader.read_footprint(path)
    assert len(parsed.pads) == 79
    assert lga.netlist_hints(parsed.pads, gnd_pads=LG290P_GND) == lg290p_hints()


def test_lg290p_channels_are_f2s1s_lattice():
    ch = lga.escape_channels(ft.lg290p().pads, LG290P_GND)
    assert ch["lattice"] == [5, 11] and ch["pad_size"] == [1.0, 1.0] and ch["skipped"] == []
    assert [v["at"] for v in ch["vias"]] == [[x, y] for x, y, *_ in winner_rows()]
    assert [v["corner_pads"] for v in ch["vias"]] == [list(r[2:]) for r in winner_rows()]
    assert all(v["via_size"] == [0.5, 0.25] for v in ch["vias"])
    assert all(v["gap"] == pytest.approx(0.2828, abs=1e-4) for v in ch["vias"])
    corners = {n for v in ch["vias"] for n in v["corner_pads"]}
    assert corners == {str(n) for n in range(25, 80)}       # every inner pad gets a via


def test_winner_notes_name_f2s1_numbers_and_the_frame():
    text = " ".join(lg290p_hints()["notes"])
    for part in ("F2-S1", "winner", "DRC 0 / unconnected 0", "9.0.4", "10.0.5", "12.0 s",
                 "15.3 s", "40 vias 0.5/0.25", "5 x 11", "footprint frame", "0.033 mm",
                 "F.Cu"):
        assert part in text, part


def test_regenerated_winner_netlist_is_schema_valid():
    design = {"name": "lg290p_breakout", "board": {"w": 48, "h": 32},
              "components": [{"ref": "U1", "lib_id": "p:LG290P", "footprint": "p:Quectel_LG290P",
                              "x": 21.0, "y": 16.0, "rot": 0.0}],
              "nets": {"GND": [["U1", n] for n in LG290P_GND], "+3V3": [["U1", "23"]]}}
    before = copy.deepcopy(design)
    out = lga.apply_hints(design, lg290p_hints(), ref="U1")
    assert design == before and "preroutes" not in design            # never mutated
    assert set(out) == set(design) | {"preroutes"}
    assert_preroutes_close(out["preroutes"], winner_preroutes())
    assert netlist_schema.validate(out) == []


# ---------------------------------------------------------------------------
# Determinism and the synthetic grid
# ---------------------------------------------------------------------------
def test_hints_are_deterministic_for_any_pad_order():
    pads = list(ft.lg290p().pads)
    want = json.dumps(lg290p_hints(), sort_keys=True)
    for seed in range(3):
        shuffled = pads[:]
        random.Random(seed).shuffle(shuffled)
        got = lga.netlist_hints(shuffled, gnd_pads=reversed(LG290P_GND))
        assert json.dumps(got, sort_keys=True) == want
    vias = lga.escape_channels(pads, LG290P_GND)["vias"]
    assert [v["at"] for v in vias] == sorted((v["at"] for v in vias), key=lambda a: (a[1], a[0]))


def test_a_3x3_synthetic_grid_gets_four_vias():
    pads = grid(3)
    hints = lga.netlist_hints(pads, gnd_pads=[p["number"] for p in pads], ref="U7")
    pr = hints["preroutes"]
    assert len(pr) == 8 and set(hints) == {"preroutes", "notes"}
    assert [p["vias"] for p in pr[::2]] == [[[-0.7, -0.7]], [[0.7, -0.7]], [[-0.7, 0.7]],
                                           [[0.7, 0.7]]]
    assert all(p["via_size"] == [0.5, 0.25] for p in pr[::2]) and all("vias" not in p
                                                                       for p in pr[1::2])
    # pads 1..9 column-major: the top-left channel's corners are 1 (x0,y0), 4, 5, 2
    assert pr[0]["path"] == [["pad", "U7", "1"], ["xy", -0.7, -0.7], ["pad", "U7", "5"]]
    assert pr[1]["path"] == [["pad", "U7", "4"], ["xy", -0.7, -0.7], ["pad", "U7", "2"]]


def test_a_signal_pad_in_the_grid_blocks_its_channels():
    pads = grid(3)
    hints = lga.netlist_hints(pads, gnd_pads=[p["number"] for p in pads if p["number"] != "5"])
    assert set(hints) == {"notes"}                         # every channel touches pad 5
    assert any("no 2x2 ground-pad channel" in n for n in hints["notes"])


def test_a_tight_pitch_skips_and_reports_every_channel():
    pads = grid(3, pitch=1.2)                              # gap 0.141: a 0.233 mm via
    ch = lga.escape_channels(pads, [p["number"] for p in pads])
    assert ch["vias"] == [] and len(ch["skipped"]) == 4
    assert all("min_via_diameter 0.45" in s["reason"] for s in ch["skipped"])
    hints = lga.netlist_hints(pads, gnd_pads=[p["number"] for p in pads])
    assert "preroutes" not in hints
    assert sum("skipped" in n for n in hints["notes"]) >= 4


def test_fab_rules_and_max_via_size_the_vias():
    pads, gnd = grid(3), [str(n) for n in range(1, 10)]
    assert lga.escape_channels(pads, gnd, max_via=0.45)["vias"][0]["via_size"] == [0.45, 0.2]
    big_drill = lga.escape_channels(pads, gnd, fab={"min_via_drill": 0.3})
    assert big_drill["vias"] == [] and "annulus" in big_drill["skipped"][0]["reason"]
    loose = lga.escape_channels(pads, gnd, fab={"min_via_annular": 0.1})
    assert loose["vias"][0]["via_size"] == [0.5, 0.3]
    wide = lga.netlist_hints(pads, gnd_pads=gnd, fab={"min_track_width": 0.3})
    assert {p["width"] for p in wide["preroutes"]} == {0.3}
    with pytest.raises(ValueError, match=r"unknown fab rule 'min_via_diamter' \(did you mean "
                                         r"'min_via_diameter'\?\)"):
        lga.netlist_hints(pads, gnd_pads=gnd, fab={"min_via_diamter": 0.4})
    with pytest.raises(ValueError, match="min_clearance"):
        lga.fab_rules({"min_clearance": -1})


def test_foreign_copper_near_a_channel_skips_it():
    pads = grid(2) + [{"number": "9", "x": 0.0, "y": 0.0, "w": 0.2, "h": 0.2}]
    ch = lga.escape_channels(pads, ["1", "2", "3", "4"])
    assert ch["vias"] == [] and "nearer" in ch["skipped"][0]["reason"]
    # tall pads: the stub leaves pad 1 farther from the via than the corner gap, so a
    # foreign pad on it is clear of the via but not of the stub
    tall = [{"number": n, "x": x, "y": y, "w": 0.4, "h": 1.6}
            for n, x, y in (("1", -0.5, -1.0), ("2", 0.5, -1.0), ("3", 0.5, 1.0),
                            ("4", -0.5, 1.0))]
    assert lga.escape_channels(tall, ["1", "2", "3", "4"])["vias"][0]["via_size"] == [0.5, 0.25]
    pads = tall + [{"number": "9", "x": -0.25, "y": -0.5, "w": 0.05, "h": 0.05}]
    ch = lga.escape_channels(pads, ["1", "2", "3", "4"])
    assert ch["vias"] == [] and "stub" in ch["skipped"][0]["reason"]


def test_far_apart_ground_corners_are_not_a_channel():
    """An LCC whose four corner pads are GND: other pads sit nearer the centre."""
    rows = [{"number": str(i + 1), "x": -6.1, "y": -7.0 + 1.1 * i, "w": 2.5, "h": 0.8,
             "shape": "oval"} for i in range(7)]
    rows += [{**p, "number": str(15 - int(p["number"])), "x": 6.1} for p in rows]
    ch = lga.escape_channels(rows, ["1", "7", "8", "14"])
    assert ch["vias"] == [] and ch["skipped"] and "nearer" in ch["skipped"][0]["reason"]


# ---------------------------------------------------------------------------
# Strategies (a)-(e), nets and inputs
# ---------------------------------------------------------------------------
def test_strategies_a_to_e_return_their_keys():
    keys = {s: set(lg290p_hints(strategy=s)) - {"notes"} for s in lga.STRATEGIES}
    assert keys == {"a": set(), "b": {"planes"}, "c": {"preroutes"}, "d": {"emc"},
                    "e1": {"planes", "emc"}, "e2": {"planes", "preroutes"},
                    "e3": {"preroutes", "emc"}, "e4": {"preroutes", "planes"},
                    "f": {"preroutes"}}
    assert lg290p_hints(strategy="b")["planes"] == {"F.Cu": "GND"}
    assert lg290p_hints(strategy="d")["emc"] == {"stitch": True}
    assert lg290p_hints(strategy="e1", emc_max_freq_hz=1.61e9)["emc"] == {
        "stitch": True, "max_freq_hz": 1.61e9}
    assert lg290p_hints(strategy="e3")["preroutes"] == lg290p_hints()["preroutes"]
    assert lg290p_hints(strategy="e2_fcu_zone_designer_vias") == lg290p_hints(strategy="e2")
    for s in ("b", "e1", "e2", "e4"):
        assert any("FAIL" in n for n in lg290p_hints(strategy=s)["notes"]), s
    with pytest.raises(ValueError, match=r"unknown strategy 'g'.*f_escape_vias"):
        lg290p_hints(strategy="g")


def test_strategy_c_reproduces_f2s1s_designer_vias():
    """F2-S1's hand-written (c) vias, sorted by (y, x). F2-S1 stubbed the top-right via to
    pad "75" (2.8, 1.4), a 9.4 mm stub across the grid; the corner pad there is 69 (2.8, -7)."""
    pr = lg290p_hints(strategy="c")["preroutes"]
    assert [(p["path"][0][2], p["vias"][0]) for p in pr] == [
        ("25", [-3.75, -7.95]), ("69", [3.75, -7.95]), ("35", [-3.75, 7.95]),
        ("79", [3.75, 7.95])]
    assert all(p["via_size"] == [0.6, 0.3] and p["width"] == 0.3 and p["layer"] == "F.Cu"
               and p["path"][1] == ["xy", *p["vias"][0]] for p in pr)


def test_gnd_net_and_gnd_pads():
    hints = lg290p_hints(gnd_net="GNDA", strategy="e4")
    assert {p["net"] for p in hints["preroutes"]} == {"GNDA"}
    assert hints["planes"] == {"F.Cu": "GNDA"}
    netted = [{**p, "net": "GND" if p["number"] != "5" else "SIG"} for p in grid(3)]
    assert lga.netlist_hints(netted) == lga.netlist_hints(grid(3), gnd_pads=[
        str(n) for n in range(1, 10) if n != 5])
    with pytest.raises(ValueError, match="no pad record carries net 'GND'.*gnd_pads"):
        lga.netlist_hints(grid(3))
    with pytest.raises(ValueError, match=r"not pads of this footprint: \['99'\]"):
        lga.netlist_hints(grid(3), gnd_pads=["1", "99"])
    with pytest.raises(ValueError, match="pad '1' needs x, y, w and h"):
        lga.netlist_hints([{"number": "1", "x": 0}], gnd_pads=["1"])


def test_pad_records_skip_padless_copper_and_read_shapes():
    recs = lga.pad_records([*ft.lg290p().pads, ft.Pad("", 0, 0, 1, 1, layers=("F.Paste",))])
    assert len(recs) == 79 and {r.shape for r in recs} == {"rect", "circle"}
    assert lga.pad_gap(0.0, 0.0, lga.PadRec("1", 2.0, 0.0, 2.0, 1.0, "oval")) == pytest.approx(
        1.0)
    assert lga.pad_gap(0.0, 2.0, lga.PadRec("1", 0.0, 0.0, 2.0, 1.0, "rect", 90.0)) == (
        pytest.approx(1.0))


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rot, want", [(0, (7.2, 13.0)), (90, (3.0, 22.8)), (-90, (17.0, 17.2)),
                                       (180, (12.8, 27.0)), (30, (4.075129, 15.337822))])
def test_place_point_uses_kicads_rotation(rot, want):
    # pad 25 (-2.8, -7.0) of an LG290P at (10, 20): pcbnew 9.0.4 pad positions
    assert lga.place_point(-2.8, -7.0, (10.0, 20.0), rot) == pytest.approx(want, abs=1e-4)


def test_place_hints_maps_xy_and_vias_and_renames_the_ref():
    hints = lg290p_hints(strategy="e4")
    before = copy.deepcopy(hints)
    placed = lga.place_hints(hints, origin=(30.0, 10.0), rot=90.0, ref="U3")
    assert hints == before                                          # never mutated
    first = placed["preroutes"][0]
    assert first["path"][0] == ["pad", "U3", "25"]
    assert first["path"][1] == ["xy", 23.7, 12.1] and first["vias"] == [[23.7, 12.1]]
    assert placed["planes"] == hints["planes"] and placed["notes"] == hints["notes"]


def test_apply_hints_merges_planes_emc_and_preroutes():
    design = {"components": [{"ref": "U1", "at": [21.0, 16.0, 90.0], "footprint": "p:f"}],
              "nets": {"GND": [], "VIN": [], "+3V3": []}, "emc": {"teardrops": True},
              "preroutes": [{"net": "VIN", "width": 1.0, "path": []}]}
    before = copy.deepcopy(design)
    out = lga.apply_hints(design, lg290p_hints(strategy="e1"), ref="U1")
    assert design == before
    assert out["planes"] == {"In1.Cu": "GND", "In2.Cu": "+3V3", "F.Cu": "GND"}
    assert out["emc"] == {"teardrops": True, "stitch": True}
    assert out["preroutes"] == design["preroutes"]
    out = lga.apply_hints(design, lg290p_hints(), ref="U1")
    assert len(out["preroutes"]) == 81 and out["preroutes"][1]["vias"] == [[14.7, 18.1]]
    with pytest.raises(ValueError, match="no component 'U9'"):
        lga.apply_hints(design, lg290p_hints(), ref="U9")
    flipped = {"components": [{"ref": "U1", "x": 1, "y": 2, "side": "B"}]}
    with pytest.raises(ValueError, match="front-side"):
        lga.apply_hints(flipped, lg290p_hints(), ref="U1")
    with pytest.raises(ValueError, match="no placement"):
        lga.apply_hints({"components": [{"ref": "U1"}]}, lg290p_hints(), ref="U1")


@pytest.mark.parametrize("design", [
    {}, {"nets": {"GND": [], "VIN": [], "+5V": []}}, {"nets": {"GND": [], "SDA": []}},
    {"nets": {"VBAT": [], "GND": []}}, {"planes": {"In1.Cu": "GND"}}, {"planes": {}}])
def test_builder_default_planes_match_build_features(design):
    assert lga.builder_default_planes(design) == build_features.default_planes(design)


# ---------------------------------------------------------------------------
# Rules: fab floors, stdlib only, size
# ---------------------------------------------------------------------------
def test_default_fab_rules_are_the_builders_floors():
    tree = ast.parse((TOOLS / "swig_build_board.py").read_text(encoding="utf-8"))
    node = next(n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == "FAB_RULES_MM" for t in n.targets))
    rules = ast.literal_eval(node)
    assert lga.DEFAULT_FAB["min_via_diameter"] == rules["m_ViasMinSize"]
    assert lga.DEFAULT_FAB["min_via_drill"] == rules["m_MinThroughDrill"]
    assert lga.DEFAULT_FAB["min_clearance"] == rules["m_MinClearance"]
    assert lga.DEFAULT_FAB["min_track_width"] == rules["m_TrackMinWidth"]
    assert lga.DEFAULT_FAB["min_via_annular"] == pytest.approx(
        (rules["m_ViasMinSize"] - rules["m_MinThroughDrill"]) / 2)


def test_module_is_stdlib_only_and_under_400_lines():
    src = (TOOLS / "lga_strategy.py").read_text(encoding="utf-8")
    assert len(src.splitlines()) < 400
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    assert names and names <= set(sys.stdlib_module_names) | {"__future__"}, names


# ---------------------------------------------------------------------------
# Live tier: KiCad 9's Python and pcbnew
# ---------------------------------------------------------------------------
def _kicad9_python() -> str | None:
    found = next((i.python for i in env.discover()
                  if i.version_tuple[0] == 9 and i.python is not None), None)
    return str(found) if found else None


def _oldest_pcbnew_python() -> str | None:
    found = sorted((i for i in env.discover() if i.python is not None),
                   key=lambda i: i.version_tuple)
    return str(found[0].python) if found else None


def _run(python: str, code: str, *args: str) -> str:
    r = subprocess.run([python, "-c", code, str(TOOLS), *args], capture_output=True,
                       stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip().splitlines()[-1]


def test_live_kicad9_python_imports_and_runs_lga_strategy():
    python = _kicad9_python()
    if python is None:
        pytest.skip("no KiCad 9 bundled Python (3.11) to import lga_strategy with")
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import lga_strategy as lga, "
            "fp_author_templates as ft; gnd = json.loads(sys.argv[2]); "
            "h = lga.netlist_hints(ft.lg290p().pads, gnd_pads=gnd); "
            "print(json.dumps([sys.version_info[:2], h], sort_keys=True))")
    version, hints = json.loads(_run(python, code, json.dumps(LG290P_GND)))
    assert version == [3, 11]
    assert hints == json.loads(json.dumps(lg290p_hints()))


def test_live_pcbnew_rotates_pads_like_place_point(tmp_path):
    python = _oldest_pcbnew_python()
    if python is None:
        pytest.skip("no KiCad Python with pcbnew")
    lib = fa.write_footprint(ft.lg290p(), tmp_path / "parts.pretty").parent
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import kicad_quiet; "
            "import pcbnew\nout = {}\nfor rot in (0, 90, -90, 180, 30):\n"
            "    fp = pcbnew.FootprintLoad(sys.argv[2], 'Quectel_LG290P')\n"
            "    fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(10), pcbnew.FromMM(20)))\n"
            "    fp.SetOrientationDegrees(rot)\n"
            "    out[rot] = {p.GetNumber(): [pcbnew.ToMM(p.GetPosition().x), "
            "pcbnew.ToMM(p.GetPosition().y)] for p in fp.Pads()}\n"
            "print(json.dumps(out))")
    got = json.loads(_run(python, code, str(lib)))
    pads = {p.number: p for p in ft.lg290p().pads}
    for rot, positions in got.items():
        assert len(positions) == 79
        for n, xy in positions.items():
            want = lga.place_point(pads[n].x, pads[n].y, (10.0, 20.0), float(rot))
            assert xy == pytest.approx(want, abs=1e-4), (rot, n)
