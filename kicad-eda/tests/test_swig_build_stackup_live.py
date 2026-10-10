"""swig_build_board on the stackup (D7-14, ADR D7.5, D7.2.3, D7.2.6, D7.6, D7.7).

venv tier (always runs): the builder no longer hard-codes 4 layers or the
pre-D7 plane heuristic, uses swig_stackup.apply_layers / finalize and
rule_areas, and stays inside its size limits.

live tier (KiCad 9.0.4 locally, 10.0.5 under the K10 recipe; skipped without a
pcbnew Python): the real builder (stdin closed) on
  cp2102n_uart (no stackup: default 4L, In1 GND / In2 +3V3), copper_layers 2
  (B.Cu mixed GND), the ADR 6L JLC06161H-2116 and 4L RO4350B hybrid fixtures,
  copper_layers 1 (B.Cu unused: no pour), the legacy shorthand with a rail
  (In1 GND / In2 +3V3, no stackup block) and rf_coupon_50r (no rail: In2 GND),
  plus CPWG pour clearances (net_specs and a fresh $KICAD_EDA_STACKUP artifact)
  and a stale artifact (a warning; the netlist wins).
Each saved board is read back: copper list and layer types, thickness, the
spliced block (stackup_kicad.compare), every pour on its role layer
(board.GetLayerName(z.GetLayer())) and filled, the antenna keep-out on every
enabled copper layer with no fill under it, the pour local clearances. A
verification problem makes the build ok false (main run with finalize
reporting one).
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

import stackup_kicad as sk
import stackup_model as sm
import swig_stackup as ss
import toolchain as tc
from conftest import load_example
from fixtures import swig_python as sp

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "server" / "tools"
BUILDER = TOOLS / "swig_build_board.py"
STACKUPS = ROOT / "tests" / "fixtures" / "stackups"
BUILD_TIMEOUT_S = 600
ENV_DROP = ("KICAD_EDA_OUTLINE", "PACK_SCALE", ss.ARTIFACT_ENV)
CU2, CU4 = ["F.Cu", "B.Cu"], ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
CU6 = ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]
HEADER = "Connector_PinHeader_2.54mm:PinHeader_1x{:02d}_P2.54mm_Vertical"


# ---------------------------------------------------------------------------
# venv tier
# ---------------------------------------------------------------------------
def test_builder_takes_layers_and_planes_from_the_stackup():
    src = BUILDER.read_text(encoding="utf-8")
    feats = (TOOLS / "build_features.py").read_text(encoding="utf-8")
    assert "SetCopperLayerCount(4)" not in src and "ss.apply_layers(board, rs)" in src
    assert "ss.finalize(" in src and "ss.load_or_resolve(" in src
    assert "stackup.planes()" in src and "default_planes(design)" not in src + feats
    assert "board_shapes.add_keepout(" not in src + feats            # add_keepout's last caller
    assert "rule_areas.add_rule_area(" in feats
    assert len(src.splitlines()) < 650 and len(feats.splitlines()) < 450


# ---------------------------------------------------------------------------
# live tier: designs
# ---------------------------------------------------------------------------
def _fixture(name: str) -> dict:
    return json.loads((STACKUPS / f"{name}.json").read_text(encoding="utf-8"))["design"]


def _header_design(name: str, nets: list[str], **extra) -> dict:
    """One pin header, pin k on nets[k-1] (auto placement, auto board size)."""
    return {"name": name, **extra,
            "components": [{"ref": "J1", "value": "HDR", "footprint": HEADER.format(len(nets))}],
            "nets": {net: [["J1", str(k)]] for k, net in enumerate(nets, 1)}}


def _cpwg(name: str, gap: float | None) -> dict:
    """2L, F.Cu mixed GND (planes) under a CPWG line; B.Cu mixed takes the first
    rail (+3V3: GND is declared, ADR D7.2.3)."""
    spec = {"impedance": 50, "line": "cpwg", "layers": ["F.Cu"],
            **({"ground_gap": gap} if gap is not None else {})}
    return _header_design(name, ["GND", "RF", "+3V3"], copper_layers=2,
                          planes={"F.Cu": "GND"}, net_specs={"RF": spec})


def _without_shield(design: dict) -> dict:
    """The stock USB-C footprints name the shield pad differently per release
    (F3-11's rpi5 test does the same); the shield net is not what this is about."""
    return {**design, "nets": {n: [p for p in conns if p != ["J1", "SH"]]
                               for n, conns in design["nets"].items()}}


def _designs() -> dict[str, dict]:
    keep = {"two_layer": {"edge": "bottom", "depth": 4.0}, "six_layer": {"edge": "top", "depth": 4},
            "hybrid": {"edge": "left", "depth": 3, "name": "ANT_HYB"},
            "single": {"edge": "right", "depth": 3}}
    designs = {"cp2102n": _without_shield(load_example("cp2102n_uart")),
               "two_layer": _fixture("default_2l"), "six_layer": _fixture("d722_6l_2116"),
               "hybrid": _fixture("d722_4l_hybrid"), "single": _fixture("single_sided"),
               "legacy_rail": _header_design("legacy_rail", ["RF_IN", "GND", "+3V3", "RF_OUT"],
                                             stackup={"h": 0.2, "er": 4.2}),
               "rf_coupon": load_example("rf_coupon_50r"),
               "cpwg": _cpwg("cpwg", 0.15), "cpwg_artifact": _cpwg("cpwg_artifact", None),
               "stale": _fixture("d722_4l_hybrid")}
    return {k: ({**d, "antenna_keepout": keep[k]} if k in keep else d) for k, d in designs.items()}


def _artifacts(work: Path, designs: dict[str, dict]) -> dict[str, Path]:
    """$KICAD_EDA_STACKUP per board: a fresh artifact with a solved CPWG line
    for cpwg_artifact, a stale one (another design's stackup) for stale."""
    def write(name: str, stackup: dict, lines: dict) -> Path:
        path = work / f"{name}.stackup.json"
        path.write_text(json.dumps({"schema": ss.ARTIFACT_SCHEMA, "stackup": stackup,
                                    "lines": lines}), encoding="utf-8")
        return path
    fresh = sm.resolve(designs["cpwg_artifact"])[0].to_dict()
    line = {"kind": "single", "line": "cpwg", "primary_layer": "F.Cu",
            "layers": {"F.Cu": {"width": 1.0, "ground_gap": 0.18}}}
    other = sm.resolve(_fixture("d722_6l_2116"))[0].to_dict()
    return {"cpwg_artifact": write("cpwg_artifact", fresh, {"RF": line}),
            "stale": write("stale", other, {})}


def _build(kpy: str, footprints: Path, netlist: Path, out: Path,
           artifact: Path | None = None) -> tuple[int, dict]:
    env = {k: v for k, v in os.environ.items() if k not in ENV_DROP}
    if artifact is not None:
        env[ss.ARTIFACT_ENV] = str(artifact)
    proc = subprocess.run([kpy, str(BUILDER), str(netlist), str(out), str(footprints)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=BUILD_TIMEOUT_S, env=env)
    result = tc.last_json(proc.stdout) or {"stdout": proc.stdout[-3000:]}
    return proc.returncode, {**result, "stderr": proc.stderr[-2000:]}


INSPECT = sp.PREAMBLE + r"""
import board_outline as bo
import rule_areas as ra

TYPES = {pcbnew.LT_SIGNAL: "signal", pcbnew.LT_POWER: "power", pcbnew.LT_MIXED: "mixed"}

def clearance(z):
    value = z.GetLocalClearance()
    return None if value is None else round(pcbnew.ToMM(int(value)), 6)

def pour(board, z, centres):
    lids = list(z.GetLayerSet().CuStack())
    fills = [z.GetFilledPolysList(l) for l in lids if z.HasFilledPolysForLayer(l)]
    return {"layer": board.GetLayerName(z.GetLayer()), "net": z.GetNetname(),
            "layers": [board.GetLayerName(l) for l in lids], "filled": bool(z.IsFilled()),
            "filled_mm2": sum(f.Area() for f in fills) / 1e12, "clearance": clearance(z),
            "under_keepout": any(f.Contains(c) for f in fills for c in centres)}

out = {}
for pcb in sys.argv[1:]:
    board = bo.load_board(pcb)
    copper = [board.GetLayerName(l) for l in board.GetEnabledLayers().CuStack()]
    areas = [z for z in board.Zones() if z.GetIsRuleArea()]
    centres = [z.Outline().BBox().Centre() for z in areas]
    out[pcb] = {"copper": copper,
                "types": {n: TYPES.get(board.GetLayerType(board.GetLayerID(n)), "other")
                          for n in copper},
                "thickness_mm": pcbnew.ToMM(board.GetDesignSettings().GetBoardThickness()),
                "pours": [pour(board, z, centres) for z in board.Zones()
                          if not z.GetIsRuleArea()],
                "rule_areas": ra.find_rule_areas(board)}
print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> dict:
    kpy = sp.require_kicad_python()
    footprints = sp.kicad_footprints(kpy, "Package_SO.pretty").parent
    work = tmp_path_factory.mktemp("stackup_builds")
    designs = _designs()
    artifacts = _artifacts(work, designs)
    boards = {}
    for name, design in designs.items():
        netlist = work / f"{name}.json"
        netlist.write_text(json.dumps(design, indent=1), encoding="utf-8")
        pcb = work / f"{name}.kicad_pcb"
        rc, res = _build(kpy, footprints, netlist, pcb, artifacts.get(name))
        boards[name] = {"rc": rc, "res": res, "pcb": pcb, "design": design}
    pcbs = [str(b["pcb"]) for b in boards.values() if b["pcb"].is_file()]
    seen = sp.run_child(INSPECT, pcbs, work, timeout=BUILD_TIMEOUT_S)
    for b in boards.values():
        b["board"] = seen.get(str(b["pcb"]))
        b["text"] = b["pcb"].read_text(encoding="utf-8") if b["pcb"].is_file() else ""
    return {"boards": boards, "kpy": kpy, "footprints": footprints, "work": work}


def _ok(built: dict, name: str) -> dict:
    b = built["boards"][name]
    assert b["rc"] == 0 and b["res"].get("ok"), b["res"]
    assert b["res"]["missing_pads"] == [] and b["board"] is not None, b["res"]
    return b


def _pours(b: dict) -> list[tuple[str, str]]:
    return [(p["layer"], p["net"]) for p in b["board"]["pours"]]


def _check_saved(b: dict, copper: list[str], types: list[str], pours: list[tuple[str, str]],
                 spliced: bool = True) -> sm.ResolvedStackup:
    """The saved board against its stackup (module doc); returns the stackup."""
    rs, _ = sm.resolve(b["design"])
    board, res = b["board"], b["res"]
    assert board["copper"] == copper and [board["types"][n] for n in copper] == types, board
    assert board["thickness_mm"] == pytest.approx(rs.board_thickness_computed, abs=1e-6)
    assert sk.compare(rs, b["text"]) == []                     # copper, types, thickness, block
    assert ("(stackup" in b["text"]) is spliced and res["stackup"]["spliced"] is spliced
    assert _pours(b) == pours and sorted(res["zones"]) == sorted(f"{a}:{n}" for a, n in pours)
    for p in board["pours"]:
        assert p["layers"] == [p["layer"]] and p["filled"] and p["filled_mm2"] > 1.0, p
    assert res["layer_roles"] == [{"layer": c.name, "role": c.role, "net": c.net,
                                   "type": c.kicad_type} for c in rs.copper]
    st = res["stackup"]
    assert st["problems"] == [] and st["copper"] == copper and st["filled"] is True
    assert (st["kicad_copper_count"], st["copper_layers"]) == (len(copper), rs.copper_layers)
    assert st["saved_thickness_mm"] == pytest.approx(rs.board_thickness_computed, abs=1e-6)
    return rs


def _check_keepout(b: dict, name: str, copper: list[str]) -> None:
    """One rule area `name` on every enabled copper layer, legacy flags, no fill under it."""
    areas = {a["name"]: a for a in b["board"]["rule_areas"]}
    assert areas[name]["layers"] == copper and areas[name]["footprint"] is None
    assert areas[name]["flags"] == {"no_tracks": False, "no_vias": True, "no_pads": True,
                                    "no_pour": True, "no_footprints": False}
    assert b["res"]["rule_areas"] == [{"name": name, "layers": copper}]
    assert not any(p["under_keepout"] for p in b["board"]["pours"]), b["board"]["pours"]


# ---------------------------------------------------------------------------
# live tier: boards
# ---------------------------------------------------------------------------
def test_live_cp2102n_default_4l(built):
    b = _ok(built, "cp2102n")
    _check_saved(b, CU4, ["signal", "power", "power", "signal"],
                 [("In1.Cu", "GND"), ("In2.Cu", "+3V3")])
    st = b["res"]["stackup"]
    assert (st["source"], st["preset"], st["from"]) == ("default", "jlcpcb:JLC04161H-7628",
                                                         "resolved")
    assert b["res"]["rule_areas"] == [] and b["res"]["antenna_keepout"] is None


def test_live_copper_layers_2_pours_b_cu_mixed_gnd(built):
    b = _ok(built, "two_layer")
    _check_saved(b, CU2, ["signal", "mixed"], [("B.Cu", "GND")])
    _check_keepout(b, "ANTENNA", CU2)


def test_live_six_layer_preset(built):
    b = _ok(built, "six_layer")
    _check_saved(b, CU6, ["signal", "power", "signal", "signal", "power", "signal"],
                 [("In1.Cu", "GND"), ("In4.Cu", "+3V3")])
    assert b["res"]["stackup"]["thickness_mm"] == pytest.approx(1.6554, abs=1e-6)
    _check_keepout(b, "ANTENNA", CU6)


def test_live_4l_rogers_hybrid(built):
    b = _ok(built, "hybrid")
    _check_saved(b, CU4, ["signal", "power", "power", "signal"],
                 [("In1.Cu", "GND"), ("In2.Cu", "+3V3")])
    assert b["res"]["stackup"]["thickness_mm"] == pytest.approx(1.3685, abs=1e-6)
    assert "RO4350B" in b["text"] and "(dielectric_constraints yes)" in b["text"]
    _check_keepout(b, "ANT_HYB", CU4)


def test_live_copper_layers_1_has_no_b_cu_pour(built):
    b = _ok(built, "single")
    _check_saved(b, CU2, ["signal", "signal"], [])
    assert b["res"]["zones"] == [] and not [p for p in b["board"]["pours"]
                                            if "B.Cu" in p["layers"]]
    assert b["res"]["layer_roles"][1]["role"] == "unused"
    _check_keepout(b, "ANTENNA", CU2)            # a rule area, on every enabled copper layer


def test_live_legacy_shorthand_in1_gnd_in2_first_rail(built):
    b = _ok(built, "legacy_rail")
    _check_saved(b, CU4, ["signal", "power", "power", "signal"],
                 [("In1.Cu", "GND"), ("In2.Cu", "+3V3")], spliced=False)
    assert b["res"]["stackup"]["source"] == "legacy"


def test_live_rf_coupon_50r_in2_is_gnd(built):
    b = _ok(built, "rf_coupon")
    _check_saved(b, CU4, ["signal", "power", "power", "signal"],
                 [("In1.Cu", "GND"), ("In2.Cu", "GND")], spliced=False)


def test_live_cpwg_pour_clearance_from_net_specs(built):
    b = _ok(built, "cpwg")
    _check_saved(b, CU2, ["mixed", "mixed"], [("F.Cu", "GND"), ("B.Cu", "+3V3")])
    by_layer = {p["layer"]: p["clearance"] for p in b["board"]["pours"]}
    assert by_layer["F.Cu"] == pytest.approx(0.15, abs=1e-6)
    assert by_layer["B.Cu"] != pytest.approx(0.15, abs=1e-6)       # no CPWG line on B.Cu
    assert b["res"]["stackup"]["pour_clearances"] == {"F.Cu": 0.15}


def test_live_cpwg_pour_clearance_from_a_fresh_artifact(built):
    b = _ok(built, "cpwg_artifact")
    _check_saved(b, CU2, ["mixed", "mixed"], [("F.Cu", "GND"), ("B.Cu", "+3V3")])
    st = b["res"]["stackup"]
    assert st["from"] == "artifact" and st["pour_clearances"] == {"F.Cu": 0.18}
    assert {p["layer"]: p["clearance"] for p in b["board"]["pours"]}["F.Cu"] == \
        pytest.approx(0.18, abs=1e-6)


def test_live_stale_artifact_is_a_warning_and_the_netlist_wins(built):
    b = _ok(built, "stale")
    _check_saved(b, CU4, ["signal", "power", "power", "signal"],
                 [("In1.Cu", "GND"), ("In2.Cu", "+3V3")])
    assert b["res"]["stackup"]["from"] == "resolved"
    assert [w for w in b["res"]["warnings"] if "stale" in w and "re-run physics_resolve" in w]


PLAN = sp.PREAMBLE + r"""
import stackup_model as sm
import swig_build_board as sbb

two = sm.resolve({"copper_layers": 2, "planes": {"F.Cu": "GND"},
                  "nets": {"GND": [], "+3V3": []}})[0]          # F mixed GND, B mixed +3V3
four = sm.resolve({"nets": {"GND": [], "+3V3": []}})[0]          # default 4L: no mixed layer
cpwg = lambda gap, **kw: {"impedance": 50, "ground_gap": gap, **kw}
lines = {"RF": {"line": "cpwg", "layers": {"F.Cu": {"ground_gap": 0.2},
                                           "B.Cu": {"ground_gap": 0.25}}},
         "USB": {"line": "microstrip", "layers": {"F.Cu": {"ground_gap": 0.05}}},
         "BAD": {"line": "cpwg", "layers": {"F.Cu": {"ground_gap": None}}}, "X": 3}
specs = {"A": cpwg(0.3, line="cpwg"), "B": cpwg(0.12, freq_hz=9.15e8),
         "C": cpwg(0.1, line="cpwg", layers=["B.Cu", ["odd"]]), "D": cpwg(0.01),
         "E": cpwg(0.02, line="stripline"), "F": cpwg(True, line="cpwg"),
         "G": cpwg(0.08, freq_hz=1e9, layers=["In1.Cu"])}
print(json.dumps({
    "artifact": sbb.pour_clearances({}, two, lines),
    "declared": sbb.pour_clearances({"net_specs": specs}, two, {}),
    "both": sbb.pour_clearances({"net_specs": {"A": cpwg(0.15, line="cpwg")}}, two, lines),
    "no_mixed": sbb.pour_clearances({"net_specs": specs}, four, lines),
    "junk": sbb.pour_clearances({"net_specs": [1, 2]}, two, {"RF": {"line": "cpwg",
                                                                    "layers": [1]}}),
    "roles": sbb.layer_roles(two)}))
"""


def test_live_pour_clearance_plan(tmp_path):
    res = sp.run_child(PLAN, [], tmp_path)
    assert res["artifact"] == {"F.Cu": 0.2, "B.Cu": 0.25}     # microstrip / null gaps ignored
    # explicit cpwg (A 0.3), auto + freq_hz on the default F.Cu (B 0.12), cpwg on B.Cu (C);
    # no line + no freq_hz (D), stripline (E), a bool (F) and an inner layer (G) do not count
    assert res["declared"] == {"F.Cu": 0.12, "B.Cu": 0.1}
    assert res["both"] == {"F.Cu": 0.15, "B.Cu": 0.25}
    assert res["no_mixed"] == {} and res["junk"] == {}
    assert res["roles"] == [{"layer": "F.Cu", "role": "mixed", "net": "GND", "type": "mixed"},
                            {"layer": "B.Cu", "role": "mixed", "net": "+3V3", "type": "mixed"}]


MISMATCH = sp.PREAMBLE + r"""
import swig_build_board as sbb

real = sbb.ss.finalize

def finalize(*args, **kwargs):
    res = real(*args, **kwargs)
    return {**res, "problems": [*res["problems"], "injected: In1.Cu is LT_SIGNAL"]}

sbb.ss.finalize = finalize
sys.argv = ["swig_build_board.py", *sys.argv[1:]]
sbb.main()
"""


def test_live_a_verification_problem_makes_the_build_fail(built, tmp_path):
    netlist = tmp_path / "mismatch.json"
    netlist.write_text(json.dumps(_fixture("default_2l")), encoding="utf-8")
    res = sp.run_child(MISMATCH, [netlist, tmp_path / "mismatch.kicad_pcb",
                                  built["footprints"]], tmp_path, timeout=BUILD_TIMEOUT_S)
    assert res["ok"] is False and res["missing_pads"] == []
    assert res["error"] == "stackup verification failed: injected: In1.Cu is LT_SIGNAL"
    assert res["stackup"]["problems"] == ["injected: In1.Cu is LT_SIGNAL"]


def test_live_a_stackup_that_does_not_resolve_is_refused(built, tmp_path):
    design = copy.deepcopy(_fixture("default_2l"))
    design["planes"] = {"In1.Cu": "GND"}                       # ST-E6 on a 2-layer board
    netlist = tmp_path / "bad.json"
    netlist.write_text(json.dumps(design), encoding="utf-8")
    out = tmp_path / "bad.kicad_pcb"
    rc, res = _build(built["kpy"], built["footprints"], netlist, out)
    assert rc == 1 and res["ok"] is False and "trace" not in res, res
    assert res["error"].startswith("stackup: ") and "ST-E6" in res["error"]
    assert not out.exists()
