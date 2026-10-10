"""Post-route GND pours and EMC stitching follow the layer roles (D7-20a; ADR
D7.6 'realising the gap', D7.8 R22 / R26, D7.2.6).

swig_import_ses.py takes an optional argv[3] <stem>.stackup.json; emc_stitch.py
an optional --stackup=<stem>.stackup.json. Without them both behave as before
(tests/test_post_route_outline_live.py and tests/test_emc_stitch_outline_live.py
pin that, goldens included).

venv tier (pcbnew absent: both scripts import their pure helpers):
  * the ground net comes from the roles (GND, else a ground-named plane / mixed
    net, never a supply rail);
  * the unify pours go only on outer signal / mixed layers lacking one (2L B.Cu
    mixed GND -> F.Cu only), never on 'unused' (single-sided B.Cu) or a plane
    layer, with a local clearance = the smallest CPWG ground_gap on the layer;
  * the stitching GND layers (role layers of the net + outer pours), the
    substrate Er under F.Cu (series mean of its plies), the single-sided refusal
    and the --stackup option.

live tier (KiCad 9.0.4 locally, 10.0.5 under the K10 recipe; skipped without a
KiCad Python that imports pcbnew). Boards built with pcbnew, filled, then the
real scripts (stdin closed) and kicad-cli DRC (every severity):
  (a) 2L B.Cu mixed GND: the unify pour goes on F.Cu only, at the CPWG gap;
      stitching vias each tie >= 2 GND fills: DRC 0 errors, 0 via_dangling;
  (b) single-sided: no pour on B.Cu (the legacy run pours one), stitching is
      refused and the board is left byte for byte;
  (c) the 4L default (In1 GND, In2 +3V3): pours on F.Cu and B.Cu, GND layers
      F.Cu / In1.Cu / B.Cu, DRC clean;
  (d) the one-layer-fill probe (F.Cu GND fill on the left half only): the
      legacy stitch leaves dangling vias on the right half, the role-aware one
      none (via_dangling 0), every via in both fills;
  plus a ground named AGND (from the roles, no gnd argument), an artifact whose
  copper does not match the board, and an unreadable artifact.
"""
from __future__ import annotations

import collections
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import emc_stitch as es
import stackup_model as sm
import swig_import_ses as ses
import swig_stackup as ss
import toolchain as tc
from fixtures import swig_python as sp

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "server" / "tools"
STACKUPS = ROOT / "tests" / "fixtures" / "stackups"
IMPORT_SES, EMC = TOOLS / "swig_import_ses.py", TOOLS / "emc_stitch.py"
TIMEOUT_S = 300
ERROR_LINE = re.compile(r"(^|\s)Error:", re.M)
CPWG_GAP = 0.18
DEFAULT_ZONE_CLEARANCE = 0.5          # a new pcbnew ZONE's local clearance (9.0.4, 10.0.5)
LINE_LIMIT = 800                      # RULES.md: every *.py file < 800 lines

DESIGNS = {
    "two": {"copper_layers": 2, "nets": {"GND": [], "SIG": []}},          # B.Cu mixed GND
    "single": {"copper_layers": 1, "nets": {"GND": [], "SIG": []}},       # B.Cu unused
    "four": {"nets": {"GND": [], "+3V3": [], "SIG": []}},                 # In1 GND, In2 +3V3
    "agnd": {"copper_layers": 2, "planes": {"B.Cu": "AGND"}, "nets": {"AGND": [], "SIG": []}},
    "rail_b": {"copper_layers": 2, "planes": {"B.Cu": "+5V"}, "nets": {"+5V": [], "SIG": []}},
    "vss": {"planes": {"In1.Cu": "+5V", "In2.Cu": "VSS"}, "nets": {"+5V": [], "VSS": []}},
    "rail_first": {"planes": {"In1.Cu": "+3V3", "In2.Cu": "GND"},
                   "nets": {"+3V3": [], "GND": []}},
    "f_mixed": {"copper_layers": 2, "planes": {"F.Cu": "GND"}, "nets": {"GND": [], "+3V3": []}},
    "b_plane": {"nets": {"GND": []}, "stackup": {"layers": [
        {"type": "copper", "role": "signal"},
        {"type": "dielectric", "kind": "core", "material": "generic.fr4", "thickness": 1.51,
         "dk": 4.5},
        {"type": "copper", "role": "plane", "net": "GND"}]}},
}
LINES = {"SIG": {"kind": "single", "line": "cpwg", "primary_layer": "F.Cu",
                 "layers": {"F.Cu": {"width": 0.25, "ground_gap": CPWG_GAP}}},
         "RF2": {"line": "cpwg", "layers": {"F.Cu": {"ground_gap": 0.2},
                                            "B.Cu": {"ground_gap": 0.25}}},
         "USB": {"line": "microstrip", "layers": {"F.Cu": {"ground_gap": 0.05}}},
         "BAD": {"line": "cpwg", "layers": {"F.Cu": {"ground_gap": None}, "In1.Cu": "x"}},
         "JUNK": 3}


def _rs(name: str) -> sm.ResolvedStackup:
    rs, issues = sm.resolve(DESIGNS[name])
    assert rs is not None, issues
    return rs


def _fixture_rs(name: str) -> sm.ResolvedStackup:
    design = json.loads((STACKUPS / f"{name}.json").read_text(encoding="utf-8"))["design"]
    return sm.resolve(design)[0]


# =============================================================================
# venv tier
# =============================================================================
@pytest.mark.parametrize(("name", "gnd"), [
    ("two", "GND"), ("four", "GND"), ("rail_first", "GND"), ("f_mixed", "GND"),
    ("agnd", "AGND"), ("vss", "VSS"),
    ("rail_b", "GND"),            # +5V is a supply rail, never the ground: the default
    ("single", "GND")])           # no plane / mixed net at all: the default
def test_the_ground_net_comes_from_the_roles(name: str, gnd: str) -> None:
    assert ses.ground_net(_rs(name)) == gnd


def test_cpwg_gaps_are_the_smallest_solved_ground_gap_per_layer() -> None:
    assert ses.cpwg_gaps(LINES) == {"F.Cu": CPWG_GAP, "B.Cu": 0.25}
    assert ses.cpwg_gaps({}) == {} and ses.cpwg_gaps({"A": {"line": "cpwg", "layers": [1]}}) == {}


@pytest.mark.parametrize(("name", "existing", "layers", "skipped"), [
    ("two", {("B.Cu", "GND")}, ("F.Cu",), ["B.Cu"]),          # 2L B mixed -> F.Cu only
    ("two", set(), ("F.Cu", "B.Cu"), []),                       # a mixed GND layer lacking one
    ("single", set(), ("F.Cu",), ["B.Cu"]),                     # never on 'unused'
    ("four", {("In1.Cu", "GND"), ("In2.Cu", "+3V3")}, ("F.Cu", "B.Cu"), []),
    ("f_mixed", {("F.Cu", "GND"), ("B.Cu", "+3V3")}, (), ["F.Cu", "B.Cu"]),
    ("b_plane", set(), ("F.Cu",), ["B.Cu"]),                    # a plane layer: never
    ("agnd", {("B.Cu", "AGND")}, ("F.Cu",), ["B.Cu"]),
])
def test_unify_pours_only_outer_signal_or_mixed_layers_lacking_one(
        name: str, existing: set[tuple[str, str]], layers: tuple[str, ...],
        skipped: list[str]) -> None:
    plan = ses.pour_plan(_rs(name), existing, {})
    assert plan.layers == layers and plan.clearances == {}
    assert [s.split(":")[0] for s in plan.skipped] == skipped, plan.skipped
    assert plan.gnd == ses.ground_net(_rs(name))


def test_unify_pour_clearance_is_the_smallest_cpwg_gap_on_its_layer() -> None:
    plan = ses.pour_plan(_rs("two"), set(), LINES)
    assert plan.layers == ("F.Cu", "B.Cu")
    assert plan.clearances == {"F.Cu": CPWG_GAP, "B.Cu": 0.25}
    only_f = ses.pour_plan(_rs("two"), {("B.Cu", "GND")}, LINES)
    assert only_f.clearances == {"F.Cu": CPWG_GAP}            # only for the poured layers
    single = ses.pour_plan(_rs("single"), set(), LINES)
    assert single.layers == ("F.Cu",) and single.clearances == {"F.Cu": CPWG_GAP}
    assert "unused" in single.skipped[0]


def test_the_artifact_copper_must_match_the_board() -> None:
    rs = _rs("four")
    assert ses.copper_mismatch(rs, ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]) is None
    msg = ses.copper_mismatch(rs, ["F.Cu", "B.Cu"])
    assert "F.Cu, In1.Cu, In2.Cu, B.Cu" in msg and "F.Cu, B.Cu" in msg


def test_read_roles_reads_a_physical_artifact(tmp_path: Path) -> None:
    path = _artifact(tmp_path, "two", LINES)
    rs, lines = ses.read_roles(path)
    assert rs.to_dict() == _rs("two").to_dict() and lines == json.loads(json.dumps(LINES))
    bad = tmp_path / "bad.stackup.json"
    bad.write_text(json.dumps({"schema": "nope"}), encoding="utf-8")
    with pytest.raises(ValueError, match="kicad-eda/physical@1"):
        ses.read_roles(bad)
    with pytest.raises(OSError):
        ses.read_roles(tmp_path / "missing.stackup.json")


def test_stitching_gnd_layers_are_the_role_layers_and_outer_pours() -> None:
    four = {("F.Cu", "GND"), ("In1.Cu", "GND"), ("In2.Cu", "+3V3"), ("B.Cu", "GND")}
    assert es.gnd_fill_layers(_rs("four"), "GND", four) == ("F.Cu", "In1.Cu", "B.Cu")
    # a plane layer counts from its role even before its pour is seen
    assert es.gnd_fill_layers(_rs("four"), "GND", set()) == ("In1.Cu",)
    assert es.gnd_fill_layers(_rs("two"), "GND", {("F.Cu", "GND")}) == ("F.Cu", "B.Cu")
    assert es.gnd_fill_layers(_rs("two"), "GND", {("F.Cu", "SIG")}) == ("B.Cu",)
    six = _fixture_rs("d722_6l_2116")         # In2.Cu is a signal layer: its GND zone is not
    assert es.gnd_fill_layers(six, "GND", {("In2.Cu", "GND"), ("B.Cu", "GND")}) == \
        ("In1.Cu", "B.Cu")
    assert es.MIN_GND_FILL_LAYERS == 2


def test_er_is_the_substrate_under_f_cu() -> None:
    assert es.substrate_er(_rs("two")) == (pytest.approx(4.5), "dielectric 1 (1 ply)")
    hybrid = _fixture_rs("d722_4l_hybrid")
    assert es.substrate_er(hybrid)[0] == pytest.approx(hybrid.dielectrics[0].plies[0].er)
    six = _fixture_rs("d722_6l_2116")           # 2116 0.127 Dk 4.16 + 2313 0.0964 Dk 4.31
    er, source = es.substrate_er(six)
    assert er == pytest.approx((0.127 + 0.0964) / (0.127 / 4.16 + 0.0964 / 4.31), rel=1e-9)
    assert 4.16 < er < 4.31 and source == "dielectric 1 (2 plies, series mean)"


def test_single_sided_boards_are_refused() -> None:
    reason = es.single_sided_refusal(_rs("single"))
    assert "single-sided" in reason and "D7.2.6" in reason
    assert es.single_sided_refusal(_rs("two")) is None
    assert es.single_sided_refusal(_rs("four")) is None


def test_parse_args_takes_the_stackup_option(tmp_path: Path) -> None:
    pcb, art = tmp_path / "b.kicad_pcb", tmp_path / "b.stackup.json"
    pcb.write_text("(kicad_pcb)\n", encoding="utf-8")
    art.write_text("{}", encoding="utf-8")
    legacy = es.parse_args(["emc_stitch.py", str(pcb), "0", "grid"])
    assert legacy["stackup"] is None and legacy["gnd_net"] == "GND" and not legacy["gnd_given"]
    for argv in (["0", "grid", f"--stackup={art}"], ["--stackup", str(art), "0", "grid"]):
        a = es.parse_args(["emc_stitch.py", str(pcb), *argv])
        assert a["stackup"] == str(art) and a["max_freq_hz"] == 0 and a["interior"] is True
    a = es.parse_args(["emc_stitch.py", str(pcb), "0", "fence", "AGND", f"--stackup={art}",
                       "--fence-cutouts"])
    assert (a["gnd_net"], a["gnd_given"], a["fence_cutouts"]) == ("AGND", True, True)
    # an empty gnd_net is 'not given' (the roles' ground wins), the later positionals still read
    a = es.parse_args(["emc_stitch.py", str(pcb), "0", "grid", "", "4.2", "0.6", "0.3",
                       f"--stackup={art}"])
    assert (a["gnd_net"], a["gnd_given"], a["er"]) == ("GND", False, 4.2)
    assert (a["via_d_mm"], a["via_drill_mm"], a["stackup"]) == (0.6, 0.3, str(art))
    with pytest.raises(ValueError, match="stackup artifact not found"):
        es.parse_args(["emc_stitch.py", str(pcb), f"--stackup={tmp_path / 'none.json'}"])
    with pytest.raises(ValueError, match="--stackup needs"):
        es.parse_args(["emc_stitch.py", str(pcb), "--stackup"])


def test_line_budgets() -> None:
    """The RULES.md file limit (< 800 lines), no tighter: D7-20b extends emc_stitch.py."""
    for script in (IMPORT_SES, EMC):
        assert len(script.read_text(encoding="utf-8").splitlines()) < LINE_LIMIT, script


# =============================================================================
# live tier
# =============================================================================
BUILD = sp.PREAMBLE + r'''
import board_outline as bo
import board_shapes as bs

mm, xy = pcbnew.FromMM, pcbnew.VECTOR2I
OX, OY, W, H = 100.0, 100.0, 40.0, 30.0


def add_pad(board: pcbnew.BOARD, net: pcbnew.NETINFO_ITEM, at: pcbnew.VECTOR2I,
            pth: bool) -> None:
    fp = pcbnew.FOOTPRINT(board)
    fp.SetPosition(at)
    board.Add(fp)
    pad = pcbnew.PAD(fp)
    pad.SetNumber("1")
    if pth:
        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        pad.SetShape(pcbnew.F_Cu, pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetDrillSize(xy(mm(0.8), mm(0.8)))
        pad.SetLayerSet(pcbnew.PAD.PTHMask())
    else:
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetShape(pcbnew.F_Cu, pcbnew.PAD_SHAPE_RECT)
        pad.SetLayerSet(pcbnew.PAD.SMDMask())
    size = 1.6 if pth else 1.0
    pad.SetSize(pcbnew.F_Cu, xy(mm(size), mm(size)))
    pad.SetPosition(at)
    pad.SetNet(net)
    fp.Add(pad)


def build(spec: dict, out: str) -> None:
    board = pcbnew.CreateEmptyBoard()
    board.SetCopperLayerCount(spec["copper"])
    bs.add_outline(board, W, H, OX, OY)
    nets = {}
    for name in spec["nets"]:
        nets[name] = pcbnew.NETINFO_ITEM(board, name)
        board.Add(nets[name])
    pt = lambda p: xy(mm(OX + p[0]), mm(OY + p[1]))
    for layer, net, width in spec.get("zones", []):
        bs.add_plane(board, board.GetLayerID(layer), nets[net], width, H, OX, OY)
    for net, at, pth in spec.get("pads", []):
        add_pad(board, nets[net], pt(at), pth)
    for net, a, b in spec.get("tracks", []):
        for end in (a, b):
            add_pad(board, nets[net], pt(end), False)
        tr = pcbnew.PCB_TRACK(board)
        tr.SetStart(pt(a))
        tr.SetEnd(pt(b))
        tr.SetWidth(mm(0.25))
        tr.SetLayer(pcbnew.F_Cu)
        tr.SetNet(nets[net])
        tr.SetLocked(True)           # a locked preroute: the SES import keeps it (D7.1)
        board.Add(tr)
    pcbnew.SaveBoard(out, board)
    board = bo.load_board(out)               # filled like a built board (ADR D7.5 step 4)
    board.BuildConnectivity()
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    pcbnew.SaveBoard(out, board)


specs = json.loads(open(sys.argv[1], encoding="utf-8").read())
for out, spec in specs.items():
    build(spec, out)
print(json.dumps({"built": sorted(specs), "version": pcbnew.Version()}))
'''

AUDIT = sp.PREAMBLE + r'''
import board_outline as bo


def clearance(z: pcbnew.ZONE) -> float | None:
    value = z.GetLocalClearance()
    return None if value is None else round(pcbnew.ToMM(int(value)), 6)


def fills(board: pcbnew.BOARD, net: str, x: int, y: int) -> list:
    p = pcbnew.VECTOR2I(x, y)
    return sorted({board.GetLayerName(lid) for z in board.Zones()
                   if not z.GetIsRuleArea() and z.GetNetname() == net
                   for lid in z.GetLayerSet().CuStack()
                   if z.HasFilledPolysForLayer(lid) and z.HitTestFilledArea(lid, p, 0)})


board, net = bo.load_board(sys.argv[1]), sys.argv[2]
vias = [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA" and t.GetNetname() == net]
print(json.dumps({
    "version": pcbnew.Version(),
    "zones": sorted([board.GetLayerName(z.GetLayer()), z.GetNetname(), clearance(z),
                     bool(z.IsFilled())] for z in board.Zones() if not z.GetIsRuleArea()),
    "vias": [{"x": pcbnew.ToMM(v.GetPosition().x) - 100.0,
              "fills": fills(board, net, v.GetPosition().x, v.GetPosition().y)} for v in vias]}))
'''

GND_PTH = ("GND", (4.0, 26.0), True)
SIG = ("SIG", (10.0, 15.0), (30.0, 15.0))
BOARDS = {
    "two": {"copper": 2, "nets": ["GND", "SIG"], "zones": [("B.Cu", "GND", 40.0)],
            "pads": [GND_PTH], "tracks": [SIG]},
    "single": {"copper": 2, "nets": ["GND", "SIG"], "pads": [GND_PTH], "tracks": [SIG]},
    "four": {"copper": 4, "nets": ["GND", "+3V3", "SIG"],
             "zones": [("In1.Cu", "GND", 40.0), ("In2.Cu", "+3V3", 40.0)],
             "pads": [GND_PTH, ("+3V3", (36.0, 26.0), True)], "tracks": [SIG]},
    # F.Cu GND fill on the left half only: right of x = 20 only B.Cu carries GND fill
    "probe": {"copper": 2, "nets": ["GND", "SIG"],
              "zones": [("B.Cu", "GND", 40.0), ("F.Cu", "GND", 20.0)], "pads": [GND_PTH]},
    "agnd": {"copper": 2, "nets": ["AGND", "SIG"], "zones": [("B.Cu", "AGND", 40.0)],
             "pads": [("AGND", (4.0, 26.0), True)], "tracks": [SIG]},
}
ARTIFACTS = {"two": ("two", LINES), "single": ("single", LINES), "four": ("four", {}),
             "probe": ("two", {}), "agnd": ("agnd", {})}
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


def _artifact(work: Path, design: str, lines: dict, stem: str | None = None) -> Path:
    """<stem>.stackup.json as physics_resolve writes it (the parts these scripts read)."""
    path = work / f"{stem or design}.stackup.json"
    path.write_text(json.dumps({"schema": ss.ARTIFACT_SCHEMA, "stackup": _rs(design).to_dict(),
                                "lines": lines}), encoding="utf-8")
    return path


def _run(argv: list) -> tuple[int, dict, str]:
    try:
        proc = subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                              timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        pytest.fail(f"KiCad child hung for {TIMEOUT_S} s: {e.stdout!r} {e.stderr!r}")
    return proc.returncode, tc.last_json(proc.stdout) or {}, proc.stdout + proc.stderr


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict:
    kpy = sp.require_kicad_python()
    root = tmp_path_factory.mktemp("unify_roles")
    pcbs = {name: root / name / "board.kicad_pcb" for name in BOARDS}
    for pcb in pcbs.values():
        pcb.parent.mkdir()
        pcb.with_suffix(".ses").write_text(MIN_SES, encoding="utf-8")
    specs = root / "specs.json"
    specs.write_text(json.dumps({str(pcbs[n]): s for n, s in BOARDS.items()}), encoding="utf-8")
    sp.run_child(BUILD, [specs], root)
    arts = {name: _artifact(root, *ARTIFACTS[name], stem=name) for name in BOARDS}
    return {"kpy": kpy, "root": root, "pcbs": pcbs, "artifacts": arts}


def _copy(built: dict, name: str, tmp_path: Path, tag: str) -> Path:
    out = tmp_path / tag / "board.kicad_pcb"
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(built["pcbs"][name], out)
    shutil.copyfile(built["pcbs"][name].with_suffix(".ses"), out.with_suffix(".ses"))
    return out


def _import_ses(built: dict, pcb: Path, artifact: Path | None = None) -> tuple[int, dict]:
    rc, res, out = _run([built["kpy"], IMPORT_SES, pcb, pcb.with_suffix(".ses"),
                         *([artifact] if artifact else [])])
    assert res, out
    return rc, res


def _stitch(built: dict, pcb: Path, *args: str) -> tuple[int, dict]:
    rc, res, out = _run([built["kpy"], EMC, pcb, "0", "grid", *args])
    assert res, out
    return rc, res


def _audit(built: dict, pcb: Path, net: str = "GND") -> dict:
    return sp.run_child(AUDIT, [pcb, net], pcb.parent)


def _drc(pcb: Path) -> dict:
    """kicad-cli DRC, every severity: error-severity violations, via_dangling of
    any severity and unconnected items (a fresh report, no Error: line)."""
    cli = tc.board_cli(str(pcb))
    assert cli, "no kicad-cli for this board"
    rpt = pcb.with_name(f"{pcb.stem}.drc.json")
    rpt.unlink(missing_ok=True)
    rc, _res, out = _run([cli, "pcb", "drc", pcb, "--format", "json", "--severity-all",
                          "-o", rpt])
    assert rc == 0 and not ERROR_LINE.search(out) and rpt.is_file(), out
    report = json.loads(rpt.read_text(encoding="utf-8"))
    viol = report.get("violations", [])
    return {"errors": sum(v.get("severity") == "error" for v in viol),
            "via_dangling": sum(v.get("type") == "via_dangling" for v in viol),
            "unconnected": len(report.get("unconnected_items", [])),
            "types": dict(collections.Counter(f"{v.get('type')}/{v.get('severity')}"
                                              for v in viol))}


def _pours(audit: dict) -> list[tuple[str, str]]:
    return [(z[0], z[1]) for z in audit["zones"]]


def _clean(pcb: Path, res: dict) -> dict:
    found = _drc(pcb)
    assert (found["errors"], found["via_dangling"], found["unconnected"]) == (0, 0, 0), \
        (found, res)
    return found


def _every_via_ties_two_fills(audit: dict) -> None:
    assert audit["vias"], audit
    thin = [v for v in audit["vias"] if len(v["fills"]) < es.MIN_GND_FILL_LAYERS]
    assert thin == [], thin


def test_live_a_two_layer_b_mixed_pours_f_cu_only_at_the_cpwg_gap(built: dict,
                                                                   tmp_path: Path) -> None:
    pcb, art = _copy(built, "two", tmp_path, "roles"), built["artifacts"]["two"]
    rc, res = _import_ses(built, pcb, art)
    assert rc == 0 and res["ok"] and res["warnings"] == [], res
    assert res["stackup"] == {"artifact": str(art), "gnd_net": "GND", "poured": ["F.Cu"],
                              "pour_clearances": {"F.Cu": CPWG_GAP},
                              "skipped": [res["stackup"]["skipped"][0]]}, res
    assert res["stackup"]["skipped"][0].startswith("B.Cu:"), res
    found = _audit(built, pcb)
    assert [z[:3] for z in found["zones"]] == [
        ["B.Cu", "GND", DEFAULT_ZONE_CLEARANCE], ["F.Cu", "GND", CPWG_GAP]], found["zones"]
    assert all(z[3] for z in found["zones"])
    # without the artifact: the same F.Cu pour (B.Cu has GND) at the zone default clearance
    legacy = _copy(built, "two", tmp_path, "legacy")
    rc, old = _import_ses(built, legacy)
    assert rc == 0 and old["ok"] and "stackup" not in old, old
    assert [z[:3] for z in _audit(built, legacy)["zones"]] == [
        ["B.Cu", "GND", DEFAULT_ZONE_CLEARANCE], ["F.Cu", "GND", DEFAULT_ZONE_CLEARANCE]]
    # then stitching with the roles: every via ties both GND fills, DRC clean
    rc, st = _stitch(built, pcb, f"--stackup={art}")
    assert rc == 0 and st["ok"] and st["vias_added"] > 20 and st["gnd"] == "GND", st
    assert st["stackup"]["gnd_layers"] == ["F.Cu", "B.Cu"] and st["er"] == pytest.approx(4.5)
    _every_via_ties_two_fills(_audit(built, pcb))
    _clean(pcb, st)


def test_live_b_single_sided_never_pours_b_cu_and_refuses_stitching(built: dict,
                                                                    tmp_path: Path) -> None:
    pcb, art = _copy(built, "single", tmp_path, "roles"), built["artifacts"]["single"]
    rc, res = _import_ses(built, pcb, art)
    assert rc == 0 and res["ok"] and res["stackup"]["poured"] == ["F.Cu"], res
    assert "unused" in res["stackup"]["skipped"][0], res
    assert _pours(_audit(built, pcb)) == [("F.Cu", "GND")]
    legacy = _copy(built, "single", tmp_path, "legacy")
    assert _import_ses(built, legacy)[0] == 0
    assert _pours(_audit(built, legacy)) == [("B.Cu", "GND"), ("F.Cu", "GND")]   # as before
    before = pcb.read_bytes()
    rc, st = _stitch(built, pcb, f"--stackup={art}")
    assert rc == 1 and st["ok"] is False and "single-sided" in st["reason"], st
    assert pcb.read_bytes() == before                    # refused before the board is touched


def test_live_c_four_layer_default_stitches_f_in1_b(built: dict, tmp_path: Path) -> None:
    pcb, art = _copy(built, "four", tmp_path, "roles"), built["artifacts"]["four"]
    rc, res = _import_ses(built, pcb, art)
    assert rc == 0 and res["ok"] and res["stackup"]["poured"] == ["F.Cu", "B.Cu"], res
    assert res["stackup"]["pour_clearances"] == {} and res["stackup"]["skipped"] == []
    assert _pours(_audit(built, pcb)) == [("B.Cu", "GND"), ("F.Cu", "GND"),
                                          ("In1.Cu", "GND"), ("In2.Cu", "+3V3")]
    rc, st = _stitch(built, pcb, f"--stackup={art}")
    assert rc == 0 and st["ok"] and st["vias_added"] > 20, st
    assert st["stackup"]["gnd_layers"] == ["F.Cu", "In1.Cu", "B.Cu"], st
    assert st["er"] == pytest.approx(4.4, abs=0.05)          # 7628 prepreg under F.Cu
    _every_via_ties_two_fills(_audit(built, pcb))
    _clean(pcb, st)


def test_live_d_one_layer_fill_probe_leaves_no_dangling_via(built: dict, tmp_path: Path) -> None:
    legacy, roles = (_copy(built, "probe", tmp_path, t) for t in ("legacy", "roles"))
    rc, old = _stitch(built, legacy)
    assert rc == 0 and old["ok"] and "stackup" not in old, old
    dangling = _drc(legacy)["via_dangling"]
    assert dangling > 0, old                               # the failure mode, without roles
    rc, st = _stitch(built, roles, f"--stackup={built['artifacts']['probe']}")
    assert rc == 0 and st["ok"] and st["vias_added"] > 5, st
    assert st["stackup"]["fill_rejected"] >= dangling, (st, dangling)
    found = _audit(built, roles)
    _every_via_ties_two_fills(found)
    assert max(v["x"] for v in found["vias"]) < 20.0, found["vias"]   # the left half only
    _clean(roles, st)


def test_live_a_ground_named_agnd_comes_from_the_roles(built: dict, tmp_path: Path) -> None:
    pcb, art = _copy(built, "agnd", tmp_path, "roles"), built["artifacts"]["agnd"]
    legacy = _copy(built, "agnd", tmp_path, "legacy")
    rc, old = _import_ses(built, legacy)                    # as before: GND or nothing
    assert rc == 0 and old["ok"] and "stackup" not in old, old
    assert [w for w in old["warnings"] if w.startswith("GND pour skipped") and "'GND'" in w]
    assert _pours(_audit(built, legacy, "AGND")) == [("B.Cu", "AGND")]
    rc, res = _import_ses(built, pcb, art)
    assert rc == 0 and res["ok"] and res["warnings"] == [], res
    assert (res["stackup"]["gnd_net"], res["stackup"]["poured"]) == ("AGND", ["F.Cu"])
    assert _pours(_audit(built, pcb, "AGND")) == [("B.Cu", "AGND"), ("F.Cu", "AGND")]
    tool_shape = _copy(built, "agnd", tmp_path, "tool_shape")
    assert _import_ses(built, tool_shape, art)[0] == 0
    rc, st = _stitch(built, pcb, f"--stackup={art}")            # no gnd argument
    assert rc == 0 and st["ok"] and st["gnd"] == "AGND" and st["vias_added"] > 20, st
    _every_via_ties_two_fills(_audit(built, pcb, "AGND"))
    _clean(pcb, st)
    # add_ground_stitching's shape: an empty gnd_net, then er and the via sizes positionally
    rc, tool = _stitch(built, tool_shape, "", "4.2", "0.6", "0.3", f"--stackup={art}")
    assert rc == 0 and tool["ok"] and tool["gnd"] == "AGND", tool
    assert tool["vias_added"] == st["vias_added"], (tool, st)
    assert tool["er"] == pytest.approx(st["er"]) == pytest.approx(4.5)   # the substrate's
    # roles naming GND on a board without that net: no pour, a warning, the routing saved
    other = _copy(built, "agnd", tmp_path, "no_gnd")
    rc, res = _import_ses(built, other, built["artifacts"]["two"])
    assert rc == 0 and res["ok"] and res["stackup"]["poured"] == [], res
    assert res["warnings"] == ["GND pour skipped: no 'GND' net on the board"], res


def test_live_an_artifact_for_another_board_is_refused(built: dict, tmp_path: Path) -> None:
    pcb = _copy(built, "two", tmp_path, "mismatch")
    before = pcb.read_bytes()
    rc, res = _import_ses(built, pcb, built["artifacts"]["four"])
    assert rc == 1 and res["ok"] is False and "does not match" in res["error"], res
    rc, st = _stitch(built, pcb, f"--stackup={built['artifacts']['four']}")
    assert rc == 1 and st["ok"] is False and "does not match" in st["reason"], st
    assert pcb.read_bytes() == before


def test_live_an_unreadable_artifact_is_an_error(built: dict, tmp_path: Path) -> None:
    pcb = _copy(built, "two", tmp_path, "garbage")
    junk = tmp_path / "junk.stackup.json"
    junk.write_text("not json", encoding="utf-8")
    before = pcb.read_bytes()
    rc, res = _import_ses(built, pcb, junk)
    assert rc == 1 and res["ok"] is False and "junk.stackup.json" in res["error"], res
    rc, st = _stitch(built, pcb, f"--stackup={junk}")
    assert rc == 1 and st["ok"] is False and "junk.stackup.json" in st["reason"], st
    assert pcb.read_bytes() == before
