"""swig_heal — the G4 micro-gap healer at the net-class width (F5-03b).

Unit tier (no KiCad; pcbnew stubbed for the import):
  * DRC 'unconnected items' pairs are parsed into same-net breaks (other pairs
    are ignored), the copper of a net is grouped into islands by a geometric
    union-find (pads, track ends and T-junctions, vias, fill outlines) that
    does not depend on the order items are listed in, island pairs and anchor
    pairs sort by geometry only, heal widths follow the class (max of attach
    and class width) and unclassed nets keep the old widths, maze connectors
    are re-routed onto the grid line the maze validated.

Live tier (skipped without a KiCad 9/10 bundled Python that imports pcbnew;
runs on 9.0.4 locally and 10.0.5 in Docker): synthetic boards are built with
pcbnew, their project gets a classed net, kicad-cli DRC writes the report and
`swig_board_fixes.py <pcb> <netlist> <drc> heal` (the harness entry point)
heals them:
  (a) two islands 5 mm apart: the link is laid at the class width (an
      unclassed net keeps the old 0.3 mm link);
  (b) foreign copper leaves only a 0.3 mm corridor: nothing is laid at the
      1.0 mm class width and the open entry is returned (unclassed: 0.3 mm
      link through the corridor);
  (c) the same board built with its items added in two orders, healed from
      DRC lists in two orders, gives identical heal output and geometry;
  (d) two DRC pairs naming the same break (pad-pad and pad-track): one link;
  (e) a maze path whose naive first segment violates clearance is re-routed
      onto the validated grid line; kicad-cli DRC shows 0 clearance errors;
  plus a short classed bridge, a stitched via between layer-locked islands
  and a plane net whose pads join their zone fills.
"""
from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

import kicad_env as env

TOOLS = Path(__file__).resolve().parents[1] / "server" / "tools"
LIVE_TIMEOUT_S = 300
CLEARANCE_TYPES = {"clearance", "shorting_items", "tracks_crossing", "hole_clearance",
                   "copper_edge_clearance", "solder_mask_bridge"}


# ---------------------------------------------------------------------------
# Unit tier: the pure parts of swig_heal, pcbnew stubbed for the import
# ---------------------------------------------------------------------------
def _import_heal() -> types.ModuleType:
    try:
        import pcbnew  # noqa: F401  (KiCad's bundled python only)
    except ImportError:
        pass
    else:
        return importlib.import_module("swig_heal")
    stub = types.ModuleType("pcbnew")
    stub.ToMM = lambda nm: nm / 1e6
    stub.FromMM = lambda v: int(round(v * 1e6))
    stub.F_Cu, stub.In1_Cu, stub.B_Cu = 0, 4, 2
    saved = sys.modules.get("pcbnew")
    sys.modules["pcbnew"] = stub
    try:
        for name in ("swig_heal", "swig_copper"):
            sys.modules.pop(name, None)
        return importlib.import_module("swig_heal")
    finally:
        for name in ("swig_heal", "swig_copper"):
            sys.modules.pop(name, None)
        if saved is None:
            sys.modules.pop("pcbnew", None)
        else:
            sys.modules["pcbnew"] = saved


@pytest.fixture(scope="module")
def heal() -> types.ModuleType:
    return _import_heal()


def _drc_item(desc: str, x: float, y: float, uuid: str) -> dict:
    return {"description": desc, "pos": {"x": x, "y": y}, "uuid": uuid}


def test_drc_breaks_keeps_same_net_pairs_only(heal):
    drc = {"unconnected_items": [
        {"items": [_drc_item("Pad 1 [VIN] of C1 on F.Cu", 1, 2, "u1"),
                   _drc_item("Track [VIN] on F.Cu, length 4.9780 mm", 3, 4, "u2")]},
        {"items": [_drc_item("Pad 1 [VIN] of C1 on F.Cu", 1, 2, "u1"),
                   _drc_item("Pad 2 [GND] of C1 on F.Cu", 5, 6, "u3")]},
        {"items": [_drc_item("Pad 1 [VIN] of C1 on F.Cu", 1, 2, "u1")]},
        {"items": [_drc_item("Pad 1 of C1 on F.Cu", 1, 2, "u1"),
                   _drc_item("Pad 2 of C1 on F.Cu", 1, 2, "u4")]}]}
    assert heal.drc_breaks(drc) == [
        heal.Break("VIN", (("u1", (1.0, 2.0)), ("u2", (3.0, 4.0))))]
    assert heal.drc_breaks({}) == [] and heal.drc_breaks({"unconnected_items": None}) == []


def _pad(heal, uuid, cx, cy, half=0.5, layers=(0,)):
    return heal.pad_element(uuid, (cx, cy), (cx - half, cy - half, cx + half, cy + half),
                            frozenset(layers), pth=False)


def _net_a(heal) -> list:
    """Pad P1 + track to a T-junction on track T2 + via V1 at T2's end;
    pad P2 alone (island 2); pad P3 on B.Cu under T2 (other layer: alone)."""
    return [_pad(heal, "P1", 0, 0),
            heal.track_element("T1", (0, 0), (5, 0), 0.3, 0),
            heal.track_element("T2", (5, -3), (5, 3), 0.3, 0),
            heal.via_element("V1", (5, 3), 0.6),
            _pad(heal, "P2", 20, 0),
            _pad(heal, "P3", 5, -1, layers=(2,))]


def test_islands_join_pads_track_ends_t_junctions_and_vias(heal):
    elements = _net_a(heal)
    roots = heal.island_roots(elements)
    groups = {}
    for e, r in zip(elements, roots, strict=True):
        groups.setdefault(r, set()).add(e.uuid)
    assert sorted(map(sorted, groups.values())) == [["P1", "T1", "T2", "V1"], ["P2"], ["P3"]]


def test_islands_do_not_depend_on_item_order(heal):
    elements = _net_a(heal)
    a = heal.islands(elements)
    b = heal.islands(list(reversed(elements)))
    assert [(i.key, i.anchors) for i in a] == [(i.key, i.anchors) for i in b]
    assert [i.key for i in a] == sorted(i.key for i in a)


def test_fill_outlines_join_what_they_contain(heal):
    inside = heal.fill_element("Z1", 0, (0, 0, 10, 10), lambda p, acc: p[0] <= 10 + acc)
    elements = [inside, _pad(heal, "G1", 5, 5), _pad(heal, "G2", 30, 5),
                heal.via_element("V9", (10.2, 5), 0.6),
                heal.track_element("T9", (12, 5), (10.1, 5), 0.25, 2)]
    groups = {}
    for e, r in zip(elements, heal.island_roots(elements), strict=True):
        groups.setdefault(r, set()).add(e.uuid)
    # the B.Cu track ends inside the via (any layer); the via ring touches the fill
    assert sorted(map(sorted, groups.values())) == [["G1", "T9", "V9", "Z1"], ["G2"]]


def test_anchor_pairs_sort_by_distance_then_coordinates(heal):
    a = (heal.Anchor(0, 0, 0, 0.25),)
    b = (heal.Anchor(3, 4, 0, 0.3), heal.Anchor(3, -4, 0, 0.3), heal.Anchor(5, 0, None, 0.6))
    pairs = heal.anchor_pairs(a, b)
    assert [p[1] for p in pairs] == [b[1], b[0], b[2]]          # ties: (x, y, layer)
    maze = heal.maze_order(pairs)
    assert maze[0][1] == b[2]                                     # via: most layer freedom


def test_widths_follow_the_class_and_unclassed_keep_the_old_ones(heal):
    pad, via = heal.Anchor(0, 0, 0, 0.25), heal.Anchor(1, 0, None, 0.6)
    fat = heal.Anchor(2, 0, 0, 1.0)
    assert heal.bridge_width(None, pad, via) == 0.25
    assert heal.bridge_width(0.528, pad, via) == 0.528
    assert heal.bridge_width(0.528, fat, fat) == 1.0
    assert heal.link_width(None, pad, via) == 0.3
    assert heal.link_width(1.375, pad, via) == 1.375
    assert heal.width_note(0.528, 0.528) == " at the 0.528 mm class width"
    assert heal.width_note(None, 0.3) == " at 0.300 mm"


def test_maze_connectors_try_the_grid_line_first(heal):
    raw = [(10.17, 15.03), (19.4, 15.2), (19.4, 10.0), (30.0, 10.0)]
    variants = heal.connector_variants(raw)
    assert variants[0] == raw
    assert variants[1] == [(10.17, 15.03), (10.17, 15.2), (19.4, 15.2), (19.4, 10.0),
                           (30.0, 10.0)]
    assert len(variants) == 9 and all(v[0] == raw[0] and v[-1] == raw[-1] for v in variants)
    assert heal.connector_variants([(0.0, 0.0), (5.0, 0.0)]) == [[(0.0, 0.0), (5.0, 0.0)]]


# ---------------------------------------------------------------------------
# Live tier
# ---------------------------------------------------------------------------
BUILD = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
import kicad_quiet  # noqa: E402,F401  (before pcbnew)
import pcbnew  # noqa: E402

mm, xy = pcbnew.FromMM, pcbnew.VECTOR2I
LAYER = {"F": pcbnew.F_Cu, "B": pcbnew.B_Cu}


def pt(p):
    return xy(mm(p[0]), mm(p[1]))


def order(spec, seq):
    return list(reversed(seq)) if spec.get("reverse") else list(seq)


def add_pads(board, spec, nets, uuids):
    fps = {}
    for p in spec.get("pads", []):
        fps.setdefault(p["ref"], []).append(p)
    for ref in order(spec, sorted(fps)):
        fp = pcbnew.FOOTPRINT(board)
        fp.SetReference(ref)
        fp.SetPosition(pt(fps[ref][0]["at"]))
        board.Add(fp)
        for p in order(spec, fps[ref]):
            pad = pcbnew.PAD(fp)
            pad.SetNumber(p["num"])
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetShape(pcbnew.F_Cu, pcbnew.PAD_SHAPE_RECT)
            pad.SetSize(pcbnew.F_Cu, xy(mm(p["size"][0]), mm(p["size"][1])))
            pad.SetLayerSet(pcbnew.PAD.SMDMask())
            pad.SetPosition(pt(p["at"]))
            pad.SetNet(nets[p["net"]])
            fp.Add(pad)
            uuids[ref + "." + p["num"]] = pad.m_Uuid.AsString()


def add_copper(board, spec, nets, uuids):
    for t in order(spec, spec.get("tracks", [])):
        tr = pcbnew.PCB_TRACK(board)
        tr.SetStart(pt(t["a"]))
        tr.SetEnd(pt(t["b"]))
        tr.SetWidth(mm(t["w"]))
        tr.SetLayer(LAYER[t.get("layer", "F")])
        tr.SetNet(nets[t["net"]])
        board.Add(tr)
        uuids[t["id"]] = tr.m_Uuid.AsString()
    for z in order(spec, spec.get("zones", [])):
        zone = pcbnew.ZONE(board)
        zone.SetLayer(LAYER[z.get("layer", "F")])
        zone.SetNet(nets[z["net"]])
        x0, y0, x1, y1 = z["rect"]
        outline = zone.Outline()
        outline.NewOutline()
        for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            outline.Append(mm(px), mm(py))
        board.Add(zone)
        uuids[z["id"]] = zone.m_Uuid.AsString()


def build(spec, out):
    board = pcbnew.CreateEmptyBoard()
    edge = pcbnew.PCB_SHAPE(board)
    edge.SetShape(pcbnew.SHAPE_T_RECT)
    edge.SetStart(xy(0, 0))
    edge.SetEnd(pt(spec.get("size", (40, 30))))
    edge.SetLayer(pcbnew.Edge_Cuts)
    edge.SetWidth(mm(0.1))
    board.Add(edge)
    nets = {}
    for name in order(spec, spec["nets"]):
        nets[name] = pcbnew.NETINFO_ITEM(board, name)
        board.Add(nets[name])
    uuids = {}
    add_pads(board, spec, nets, uuids)
    add_copper(board, spec, nets, uuids)
    if not pcbnew.SaveBoard(out, board):
        raise SystemExit("SaveBoard failed: " + out)
    if spec.get("zones"):            # the headless filler needs a LOADED board
        loaded = pcbnew.LoadBoard(out)
        loaded.BuildConnectivity()
        pcbnew.ZONE_FILLER(loaded).Fill(loaded.Zones())
        pcbnew.SaveBoard(out, loaded)
    return uuids


specs = json.loads(open(sys.argv[2], encoding="utf-8").read())
print(json.dumps({out: build(spec, out) for out, spec in specs.items()}))
'''

DUMP = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
import kicad_quiet  # noqa: E402,F401  (before pcbnew)
import pcbnew  # noqa: E402

to = pcbnew.ToMM
out = {}
for pcb in sys.argv[2:]:
    board = pcbnew.LoadBoard(pcb)
    if board is None:
        raise SystemExit("LoadBoard failed: " + pcb)
    tracks, vias = [], []
    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA":
            p = t.GetPosition()
            vias.append([t.GetNetname(), round(to(p.x), 4), round(to(p.y), 4)])
        else:
            s, e = t.GetStart(), t.GetEnd()
            ends = sorted([[round(to(s.x), 4), round(to(s.y), 4)],
                           [round(to(e.x), 4), round(to(e.y), 4)]])
            tracks.append([t.GetNetname(), board.GetLayerName(t.GetLayer()),
                           round(to(t.GetWidth()), 4), ends])
    out[pcb] = {"tracks": sorted(tracks), "vias": sorted(vias)}
print(json.dumps(out))
'''


def _pad_spec(ref: str, num: str, net: str, at: tuple, size: tuple = (1.0, 1.0)) -> dict:
    return {"ref": ref, "num": num, "net": net, "at": list(at), "size": list(size)}


def _track(tid: str, net: str, a: tuple, b: tuple, w: float, layer: str = "F") -> dict:
    return {"id": tid, "net": net, "a": list(a), "b": list(b), "w": w, "layer": layer}


TWO_ISLANDS = {"nets": ["VIN"],
               "pads": [_pad_spec("U1", "1", "VIN", (10, 15)),
                        _pad_spec("U2", "1", "VIN", (20, 15))],
               "tracks": [_track("t1", "VIN", (10, 15), (13, 15), 0.6),
                          _track("t2", "VIN", (20, 15), (18, 15), 0.6)]}
CORRIDOR = {**TWO_ISLANDS, "nets": ["VIN", "GND", "SHIELD"],
            "tracks": TWO_ISLANDS["tracks"] + [
                _track("w1", "GND", (16, 4), (16, 14.4), 0.2),     # the maze window is
                _track("w2", "SHIELD", (16, 15.6), (16, 26), 0.2)]}  # the pair bbox +- 5 mm
THREE_ISLANDS = {"nets": ["VIN", "GND"],
                 "pads": [_pad_spec("A", "1", "VIN", (8, 15)),
                          _pad_spec("B", "1", "VIN", (14, 10)),
                          _pad_spec("B", "2", "VIN", (14, 20)),
                          _pad_spec("C", "1", "VIN", (24, 15)),
                          _pad_spec("C", "2", "GND", (26, 15))],
                 "tracks": [_track("b", "VIN", (14, 10), (14, 20), 0.5),
                            _track("c", "VIN", (24, 15), (20, 15), 0.5)]}
DUP_BREAK = {"nets": ["VIN"],
             "pads": [_pad_spec("P", "1", "VIN", (10, 15)), _pad_spec("Q", "1", "VIN", (20, 15))],
             "tracks": [_track("q", "VIN", (20, 15), (17, 15), 0.5)]}
SHORT_GAP = {"nets": ["VIN"],
             "pads": [_pad_spec("U1", "1", "VIN", (10, 15)), _pad_spec("U2", "1", "VIN", (20, 15))],
             "tracks": [_track("t1", "VIN", (10, 15), (14.5, 15), 0.3),
                        _track("t2", "VIN", (20, 15), (15.5, 15), 0.3)]}
LAYER_LOCKED = {"nets": ["VIN"],
                "pads": [_pad_spec("U1", "1", "VIN", (10, 15))],
                "tracks": [_track("t1", "VIN", (10, 15), (12, 15), 0.3),
                           _track("t2", "VIN", (20, 15), (18, 15), 0.3, "B")]}
PLANE = {"nets": ["GND"],
         "pads": [_pad_spec("G1", "1", "GND", (10, 15)), _pad_spec("G2", "1", "GND", (30, 15))],
         "zones": [{"id": "z1", "net": "GND", "rect": [2, 2, 18, 28]},
                   {"id": "z2", "net": "GND", "rect": [22, 2, 38, 28]}]}
# (e): pad A sits off the maze grid; the router's naive first segment slants
# 0.17 mm off the grid row it validated and would pass 0.09 mm from SIG.
MAZE = {"nets": ["VIN", "GND", "SIG"],
        "pads": [_pad_spec("A", "1", "VIN", (10.17, 15.03)), _pad_spec("B", "1", "VIN", (30, 15))],
        "tracks": [_track("wall", "GND", (20, 11), (20, 19), 0.2),
                   _track("sig", "SIG", (11.0, 14.7), (14.0, 14.7), 0.2)]}

BOARDS = {
    "a_class": (TWO_ISLANDS, {"VIN": 0.6}),
    "a_plain": (TWO_ISLANDS, {}),
    "b_class": (CORRIDOR, {"VIN": 1.0}),
    "b_plain": (CORRIDOR, {}),
    "c_fwd": (THREE_ISLANDS, {"VIN": 0.5}),
    "c_rev": ({**THREE_ISLANDS, "reverse": True}, {"VIN": 0.5}),
    "d_dup": (DUP_BREAK, {"VIN": 0.5}),
    "d_tri": (THREE_ISLANDS, {"VIN": 0.5}),
    "e_maze": (MAZE, {"VIN": 0.3}),
    "g_bridge": (SHORT_GAP, {"VIN": 0.5}),
    "h_stitch": (LAYER_LOCKED, {}),
    "f_plane": (PLANE, {}),
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


def _child(kicad: env.KiCadInstall, work: Path, script: str, *args) -> dict:
    path = work / f"child_{abs(hash(script)) % 10**8}.py"
    path.write_text(script, encoding="utf-8")
    done = _run([kicad.python, path, TOOLS, *args])
    assert done.returncode == 0, (done.stdout[-2000:], done.stderr[-3000:])
    return json.loads(done.stdout.strip().splitlines()[-1])


def _set_classes(pro: Path, widths: dict) -> None:
    """Bind each net to its own class (KiCad 9.0.4 ignores a class without a
    priority) in the project file pcbnew wrote beside the board."""
    data = json.loads(pro.read_text(encoding="utf-8"))
    ns = data.setdefault("net_settings", {})
    classes = [c for c in ns.get("classes", []) if c.get("name") == "Default"]
    patterns = []
    for i, (net, width) in enumerate(sorted(widths.items())):
        classes.append({"name": f"NC_{net}", "track_width": width, "clearance": 0.2,
                        "priority": i})
        patterns.append({"pattern": net, "netclass": f"NC_{net}"})
    ns.update(classes=classes, netclass_patterns=patterns)
    pro.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _drc(kicad: env.KiCadInstall, pcb: Path, tag: str) -> tuple[Path, dict]:
    rpt = pcb.with_name(f"{pcb.stem}_{tag}_drc.json")
    done = _run([kicad.kicad_cli, "pcb", "drc", pcb, "--severity-error", "--format", "json",
                 "-o", rpt])
    errors = [ln for ln in (done.stdout + done.stderr).splitlines()
              if ln.lstrip().startswith("Error:") or " Error:" in ln]
    assert rpt.is_file() and not errors, (done.stdout[-1500:], done.stderr[-1500:])
    return rpt, json.loads(rpt.read_text(encoding="utf-8"))


def _clearance_errors(report: dict) -> list:
    return [v for v in report.get("violations", []) if v.get("type") in CLEARANCE_TYPES]


def _heal(kicad: env.KiCadInstall, pcb: Path, drc_path: Path) -> dict:
    netlist = pcb.with_name("netlist.json")
    netlist.write_text("{}", encoding="utf-8")
    done = _run([kicad.python, TOOLS / "swig_board_fixes.py", pcb, netlist, drc_path, "heal"])
    assert done.returncode == 0, (done.stdout[-2000:], done.stderr[-3000:])
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def boards(kicad, tmp_path_factory) -> dict:
    """name -> (pcb, {label: uuid}); every board in its own directory."""
    root = tmp_path_factory.mktemp("heal")
    specs, pcbs = {}, {}
    for name, (spec, _classes) in BOARDS.items():
        (root / name).mkdir()
        pcbs[name] = root / name / "board.kicad_pcb"
        specs[str(pcbs[name])] = spec
    spec_file = root / "specs.json"
    spec_file.write_text(json.dumps(specs), encoding="utf-8")
    uuids = _child(kicad, root, BUILD, spec_file)
    for name, (_spec, classes) in BOARDS.items():
        _set_classes(pcbs[name].with_suffix(".kicad_pro"), classes)
    return {name: (pcbs[name], uuids[str(pcbs[name])]) for name in BOARDS}


def _dump(kicad: env.KiCadInstall, *pcbs: Path) -> list:
    out = _child(kicad, pcbs[0].parent, DUMP, *pcbs)
    return [out[str(p)] for p in pcbs]


def _added(before: dict, after: dict) -> list:
    rest = list(before["tracks"])
    added = []
    for t in after["tracks"]:
        if t in rest:
            rest.remove(t)
        else:
            added.append(t)
    return added


def _heal_board(kicad, boards, name: str, drc_edit=None) -> tuple:
    """DRC -> heal (through swig_board_fixes main) -> DRC. Returns
    (heal result, tracks added, post-heal DRC report, before dump, after dump)."""
    pcb, _uuids = boards[name]
    rpt, report = _drc(kicad, pcb, "pre")
    if drc_edit is not None:
        rpt.write_text(json.dumps(drc_edit(report)), encoding="utf-8")
    (before,) = _dump(kicad, pcb)
    shutil.copy(pcb, pcb.with_name("unhealed.kicad_pcb"))
    result = _heal(kicad, pcb, rpt)
    (after,) = _dump(kicad, pcb)
    _post_rpt, post = _drc(kicad, pcb, "post")
    return result, _added(before, after), post, before, after


def test_live_a_link_is_laid_at_the_class_width(kicad, boards):
    result, added, post, _b, _a = _heal_board(kicad, boards, "a_class")
    assert result == {"applied": ["VIN: linked disconnected clusters (5.00 mm apart) on F.Cu "
                                  "at the 0.600 mm class width"], "open": []}
    assert added == [["VIN", "F.Cu", 0.6, [[13.0, 15.0], [18.0, 15.0]]]]
    assert post["unconnected_items"] == [] and post["violations"] == []


def test_live_a_unclassed_net_keeps_the_old_link_width(kicad, boards):
    result, added, post, _b, _a = _heal_board(kicad, boards, "a_plain")
    assert result["applied"] == ["VIN: linked disconnected clusters (5.00 mm apart) on F.Cu "
                                 "at 0.300 mm"]
    assert added == [["VIN", "F.Cu", 0.3, [[13.0, 15.0], [18.0, 15.0]]]]
    assert post["unconnected_items"] == []


def test_live_b_no_sub_width_copper_when_only_a_narrow_corridor_is_free(kicad, boards):
    result, added, post, before, after = _heal_board(kicad, boards, "b_class")
    assert result == {"applied": [], "open": ["VIN: no clear path at the 1.000 mm class width"]}
    assert added == [] and after == before
    assert len(post["unconnected_items"]) == 1


def test_live_b_the_corridor_fits_the_unclassed_link(kicad, boards):
    result, added, post, _b, _a = _heal_board(kicad, boards, "b_plain")
    assert result["open"] == [] and len(result["applied"]) == 1
    assert all(t[2] == 0.3 for t in added) and added
    assert post["unconnected_items"] == [] and _clearance_errors(post) == []


def test_live_c_heal_output_does_not_depend_on_item_or_drc_order(kicad, boards):
    fwd = _heal_board(kicad, boards, "c_fwd")
    rev = _heal_board(kicad, boards, "c_rev", drc_edit=lambda r: {
        **r, "unconnected_items": list(reversed(r["unconnected_items"]))})
    assert fwd[0] == rev[0] and len(fwd[0]["applied"]) == 2
    assert sorted(fwd[1]) == sorted(rev[1]) and fwd[1]
    assert all(t[2] == 0.5 for t in fwd[1])
    assert fwd[2]["unconnected_items"] == [] == rev[2]["unconnected_items"]


def test_live_d_two_drc_pairs_for_one_break_get_one_link(kicad, boards):
    _pcb, uuids = boards["d_dup"]

    def two_pairs(report: dict) -> dict:
        p, q, t = (_drc_item(desc, x, y, uuids[label]) for desc, x, y, label in (
            ("Pad 1 [VIN] of P on F.Cu", 10, 15, "P.1"),
            ("Pad 1 [VIN] of Q on F.Cu", 20, 15, "Q.1"),
            ("Track [VIN] on F.Cu, length 3.0000 mm", 17, 15, "q")))
        pairs = [{"description": "Missing connection between items", "items": [p, q],
                  "severity": "error", "type": "unconnected_items"},
                 {"description": "Missing connection between items", "items": [p, t],
                  "severity": "error", "type": "unconnected_items"}]
        return {**report, "unconnected_items": pairs}

    result, added, post, _b, _a = _heal_board(kicad, boards, "d_dup", drc_edit=two_pairs)
    assert result == {"applied": ["VIN: linked disconnected clusters (7.00 mm apart) on F.Cu "
                                  "at the 0.500 mm class width"], "open": []}
    assert added == [["VIN", "F.Cu", 0.5, [[10.0, 15.0], [17.0, 15.0]]]]
    assert post["unconnected_items"] == []


def test_live_d_a_pair_an_earlier_link_joined_is_skipped(kicad, boards):
    """Three DRC pairs over three islands (A-B, B-C, A-C): two links, the
    third pair is already joined by them."""
    _pcb, uuids = boards["d_tri"]

    def triangle(report: dict) -> dict:
        a, b, c = (_drc_item(f"Pad 1 [VIN] of {ref} on F.Cu", x, y, uuids[f"{ref}.1"])
                   for ref, x, y in (("A", 8, 15), ("B", 14, 10), ("C", 24, 15)))
        pairs = [{"items": [b, c]}, {"items": [a, c]}, {"items": [a, b]}]
        return {**report, "unconnected_items": pairs}

    result, added, post, _b, _a = _heal_board(kicad, boards, "d_tri", drc_edit=triangle)
    assert result == {"applied": [
        "VIN: linked disconnected clusters (7.81 mm apart) on F.Cu at the 0.500 mm class width",
        "VIN: linked disconnected clusters (12.00 mm apart) on F.Cu at the 0.500 mm class width"],
        "open": []}
    assert sorted(added) == [["VIN", "F.Cu", 0.5, [[8.0, 15.0], [14.0, 10.0]]],
                             ["VIN", "F.Cu", 0.5, [[8.0, 15.0], [20.0, 15.0]]]]
    assert post["unconnected_items"] == []


MAZE_PROBE = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
import kicad_quiet  # noqa: E402,F401  (before pcbnew)
import pcbnew  # noqa: E402
import swig_copper as cu  # noqa: E402

board = pcbnew.LoadBoard(sys.argv[2])
nc = board.FindNet("VIN").GetNetCode()
keep = 0.3 / 2 + cu.min_clearance(board) + 0.05
raw = cu.bfs_path(board, nc, (10.17, 15.03), (30.0, 15.0), pcbnew.F_Cu, keep)
ok = [cu.clear_path(board, nc, p, q, keep, pcbnew.F_Cu, step=0.05)
      for p, q in zip(raw, raw[1:])] if raw else []
print(json.dumps({"raw": raw, "segments_clear": ok}))
'''


def test_live_e_a_maze_path_with_a_bad_first_segment_is_rerouted(kicad, boards):
    pcb, _uuids = boards["e_maze"]
    probe = _child(kicad, pcb.parent, MAZE_PROBE, pcb)
    assert probe["raw"] and probe["segments_clear"][0] is False, probe   # naive path is bad
    assert all(probe["segments_clear"][1:]), probe
    result, added, post, _b, _a = _heal_board(kicad, boards, "e_maze")
    assert result["open"] == [] and len(result["applied"]) == 1, result
    assert result["applied"][0].endswith("via maze route at the 0.300 mm class width")
    assert added and all(t[2] == 0.3 for t in added)
    assert _clearance_errors(post) == [] and post["violations"] == []
    assert post["unconnected_items"] == []


def test_live_short_gap_bridge_is_laid_at_the_class_width(kicad, boards):
    result, added, post, _b, _a = _heal_board(kicad, boards, "g_bridge")
    assert result == {"applied": ["healed 1.00 mm same-net gap on VIN (F.Cu, 0.50 mm bridge) "
                                  "at the 0.500 mm class width"], "open": []}
    assert added == [["VIN", "F.Cu", 0.5, [[14.5, 15.0], [15.5, 15.0]]]]
    assert post["unconnected_items"] == [] and post["violations"] == []


def test_live_layer_locked_islands_get_a_stitched_via(kicad, boards):
    result, added, post, _b, after = _heal_board(kicad, boards, "h_stitch")
    assert result == {"applied": ["VIN: stitched a via at (12.00,15.00) and linked clusters "
                                  "on B.Cu at 0.300 mm"], "open": []}
    assert added == [["VIN", "B.Cu", 0.3, [[12.0, 15.0], [18.0, 15.0]]]]
    assert after["vias"] == [["VIN", 12.0, 15.0]]
    assert post["unconnected_items"] == [] and _clearance_errors(post) == []


def test_live_plane_net_pads_join_their_fills(kicad, boards):
    result, added, post, _b, _a = _heal_board(kicad, boards, "f_plane")
    assert result == {"applied": ["GND: linked disconnected clusters (20.00 mm apart) on F.Cu "
                                  "at 0.300 mm — DSN plane abstraction hid the fill void"],
                      "open": []}
    assert added == [["GND", "F.Cu", 0.3, [[10.0, 15.0], [30.0, 15.0]]]]
    assert post["unconnected_items"] == [] and _clearance_errors(post) == []
