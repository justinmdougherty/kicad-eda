"""Live tier of server/tools/dsn_layers.py (D7-19): Freerouting 1.9.0 on
KiCad 9.0.4 boards with the DSN layer policy (ADR D7.9).

Each board is built by the pipeline's own builder (build_board_from_netlist),
exported by swig_export_dsn.py, post-processed by dsn_layers with the policy
the stackup model gives (the artifact's ``dsn`` section, ADR D7.4), put in
dsn_canon order and routed by board_autoroute.route (offline, -mp 600 -dct 0),
then imported by swig_import_ses.py and checked with the guarded kicad-cli DRC
(toolchain.drc_counts). A KiCad child counts the tracks per layer, the vias
and the foreign-net tracks on every plane layer.

- cp2102n_uart and stm32f103_dev (4L SIG / GND / +3V3 / SIG): 0 foreign tracks
  on In1 / In2, DRC 0 errors / 0 unconnected;
- 2L with B.Cu mixed GND (cp2102n made 2-layer, a B.Cu GND pour): signal nets
  route on both layers and the outer pour is no plane;
- single-sided (lm358_buffer made 2-layer, B.Cu unused): tracks on F.Cu only,
  no vias;
- the one-time layer_rule check (ADR D7.9) re-run on the 6L fixture: the SES
  widths per layer match LAYER_RULE_VERIFIED and the recorded evidence.

Skipped without KiCad's bundled Python (pcbnew), Java or the Freerouting jar.
"""
from __future__ import annotations

import collections
import hashlib
import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

import board_autoroute as bar
import dsn_canon
import dsn_layers as dl
import dsn_sexpr as ds
import stackup_model as sm
import toolchain as tc
from fixtures import swig_python as sp

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TOOLS = ROOT / "server" / "tools"
FIX = HERE / "fixtures" / "dsn_layers"
EXAMPLES = ROOT / "examples"
MAX_PASSES = 600
ROUTE_TIMEOUT_S = 900
CHILD_TIMEOUT_S = 300
RecordProperty = Callable[[str, object], None]   # pytest's record_property

COUNT = sp.PREAMBLE + r"""
import collections
board = pcbnew.LoadBoard(sys.argv[1])
if board is None:
    raise SystemExit(f"cannot load {sys.argv[1]}")
planes = json.loads(sys.argv[2])                 # {plane layer: its net}
per_layer, per_net, foreign, vias = collections.Counter(), {}, collections.Counter(), 0
for t in board.GetTracks():
    if t.GetClass() == "PCB_VIA":
        vias += 1
        continue
    layer, net = board.GetLayerName(t.GetLayer()), t.GetNetname()
    per_layer[layer] += 1
    per_net.setdefault(layer, collections.Counter())[net] += 1
    if layer in planes and net != planes[layer]:
        foreign[layer + " " + net] += 1
print(json.dumps({"per_layer": per_layer, "vias": vias, "foreign": sum(foreign.values()),
                  "foreign_detail": foreign, "per_net": per_net}))
"""

TO_2L = sp.PREAMBLE + r"""
board = pcbnew.LoadBoard(sys.argv[1])
if board is None:
    raise SystemExit(f"cannot load {sys.argv[1]}")
gnd = next(z for z in board.Zones() if board.GetLayerName(z.GetLayer()) == "In1.Cu")
ring = gnd.Outline().COutline(0)
points = [(ring.CPoint(i).x, ring.CPoint(i).y) for i in range(ring.PointCount())]
net = gnd.GetNet()
for z in list(board.Zones()):
    board.Remove(z)
board.SetCopperLayerCount(2)
if sys.argv[3] == "mixed":                       # B.Cu mixed GND: a full GND pour
    zone = pcbnew.ZONE(board)
    zone.SetLayer(pcbnew.B_Cu)
    zone.SetNet(net)
    outline = zone.Outline()                     # filled in place (never SetOutline)
    outline.NewOutline()
    for x, y in points:
        outline.Append(x, y)
    zone.SetLocalClearance(pcbnew.FromMM(0.3))
    board.Add(zone)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
pcbnew.SaveBoard(sys.argv[2], board)
print(json.dumps({"ok": True, "layers": board.GetCopperLayerCount(),
                  "zones": [board.GetLayerName(z.GetLayer()) for z in board.Zones()]}))
"""

IMPORT_CHECK = r"""
import hashlib
import json
import sys
sys.path.insert(0, sys.argv[1])
import dsn_layers as dl  # noqa: E402  (stdlib + dsn_sexpr: no pcbnew, no kicad_quiet needed)
raw = open(sys.argv[2], encoding="utf-8").read()
text, rep = dl.postprocess(raw, json.loads(sys.argv[3]))
print(json.dumps({"python": sys.version.split()[0], "classes": len(rep["classes"]),
                  "sha": hashlib.sha256(text.encode("utf-8")).hexdigest()}))
"""


@pytest.fixture(scope="module")
def kpy() -> str:
    return sp.require_kicad_python()


@pytest.fixture(scope="module")
def freerouting() -> tuple[str, str]:
    java, jar = tc.find_java(), tc.freerouting_jar("")
    if not java:
        pytest.skip("java not found (Freerouting needs a JRE)")
    if not jar:
        pytest.skip("Freerouting jar not found in server/tools or $FREEROUTING_JAR")
    return java, jar


def policy_of(design: dict) -> dict:
    """The artifact's dsn section for ``design`` (ADR D7.4), from the stackup
    model: routable = signal + mixed, plane-role layers per net, unused."""
    rs, _issues = sm.resolve(design)
    assert rs is not None, _issues
    planes: dict[str, list[str]] = {}
    for c in rs.copper:
        if c.role == "plane":
            planes[c.net] = [*planes.get(c.net, []), c.name]
    return {"signal_layers": list(rs.routable_layers()), "plane_nets": planes,
            "unused_layers": [c.name for c in rs.copper if c.role == "unused"],
            "net_layers": {}}


def build(example: str, size: float, folder: Path) -> Path:
    """build_board_from_netlist on a copy of examples/<example>; ok False is
    accepted only for footprint pads the netlist names but the footprint lacks
    (cp2102n's J1.SH): the board is still built and saved."""
    import board_pipeline_tools as bpt
    folder.mkdir(parents=True, exist_ok=True)
    netlist = folder / "netlist.json"
    shutil.copyfile(EXAMPLES / example / "netlist.json", netlist)
    pcb = folder / "board.kicad_pcb"
    built = bpt.build_board_from_netlist(str(netlist), size, size, out_pcb=str(pcb))
    assert built.get("ok") or (built.get("missing_pads") and not built.get("error")), built
    assert pcb.is_file() and (built.get("zones") or [""])[0] == "In1.Cu:GND", built
    return pcb


def export(kpy: str, pcb: Path) -> str:
    """KiCad's DSN export (swig_export_dsn.py, no policy argument)."""
    dsn = pcb.with_suffix(".raw.dsn")
    proc = subprocess.run([kpy, str(TOOLS / "swig_export_dsn.py"), str(pcb), str(dsn)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=CHILD_TIMEOUT_S)
    res = tc.last_json(proc.stdout) or {}
    assert proc.returncode == 0 and res.get("ok") is True, proc.stdout + proc.stderr
    return dsn.read_bytes().decode("utf-8")


def route(kpy: str, fr: tuple[str, str], pcb: Path, raw: str, policy: dict) -> dict:
    """Policy -> dsn_canon -> Freerouting -> SES import -> DRC -> track counts."""
    text, report = dl.postprocess(raw, policy)
    dsn, ses = pcb.with_suffix(".dsn"), pcb.with_suffix(".ses")
    dsn.write_bytes(text.encode("utf-8"))
    dsn_canon.canonicalize_file(dsn)
    routed = bar.route(fr[0], fr[1], str(dsn), str(ses), MAX_PASSES, ROUTE_TIMEOUT_S)
    assert routed["ok"], routed
    proc = subprocess.run([kpy, str(TOOLS / "swig_import_ses.py"), str(pcb), str(ses)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=CHILD_TIMEOUT_S)
    imported = tc.last_json(proc.stdout) or {}
    assert proc.returncode == 0 and imported.get("ok"), proc.stdout + proc.stderr
    drc = tc.drc_counts(str(pcb))
    assert "error" not in drc, drc
    planes = {layer: net for net, layers in policy["plane_nets"].items() for layer in layers}
    counts = sp.run_child(COUNT, [pcb, json.dumps(planes)], pcb.parent, CHILD_TIMEOUT_S)
    return {"report": report, "drc": drc, "counts": counts}


def summary(res: dict) -> dict:
    return {"drc_errors": res["drc"]["errors"], "unconnected": res["drc"]["unconnected"],
            "types": res["drc"]["types"], "per_layer": res["counts"]["per_layer"],
            "vias": res["counts"]["vias"], "foreign": res["counts"]["foreign_detail"]}


def design(example: str, **extra: object) -> dict:
    return {**json.loads((EXAMPLES / example / "netlist.json").read_text(encoding="utf-8")),
            **extra}


# ----------------------------------------------------------------------------
def test_live_module_runs_under_kicad_python(kpy: str, tmp_path: Path) -> None:
    pol = json.dumps({"signal_layers": ["F.Cu", "B.Cu"], "plane_nets": {
        "GND": ["In1.Cu"], "+3V3": ["In2.Cu"]}})
    script = tmp_path / "import_check.py"
    script.write_text(IMPORT_CHECK, encoding="utf-8")
    proc = subprocess.run([kpy, str(script), str(TOOLS), str(FIX / "cp2102n.dsn.txt"), pol],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=CHILD_TIMEOUT_S)
    res = tc.last_json(proc.stdout) or {}
    assert proc.returncode == 0 and res, proc.stdout + proc.stderr
    raw = (FIX / "cp2102n.dsn.txt").read_text(encoding="utf-8")
    text, rep = dl.postprocess(raw, json.loads(pol))
    assert res["python"].startswith("3.") and res["classes"] == len(rep["classes"]) == 3
    assert res["sha"] == hashlib.sha256(text.encode("utf-8")).hexdigest()   # same bytes


@pytest.mark.parametrize("example", ["cp2102n_uart", "stm32f103_dev"])
def test_live_four_layer_planes_stay_clean(kpy: str, freerouting: tuple[str, str],
                                           tmp_path: Path, example: str,
                                           record_property: RecordProperty) -> None:
    pcb = build(example, 50, tmp_path / example)
    raw = export(kpy, pcb)
    policy = policy_of(design(example))
    derived = dl.derive_policy_from_dsn(raw)
    assert (derived["signal_layers"], derived["plane_nets"]) == (
        policy["signal_layers"], policy["plane_nets"])
    res = route(kpy, freerouting, pcb, raw, policy)
    row = summary(res)
    record_property(example, row)
    print(example, json.dumps(row))
    assert res["report"]["nets_moved"] == {"+3V3": "kicad_eda_plane_+3V3",
                                           "GND": "kicad_eda_plane_GND"}
    assert res["report"]["pad_layer_excluded"] == res["report"]["pad_layer_blocked"] == {}
    assert res["counts"]["foreign"] == 0, row
    assert (row["drc_errors"], row["unconnected"]) == (0, 0), row
    assert row["per_layer"].get("F.Cu") and row["per_layer"].get("B.Cu"), row


@pytest.fixture(scope="module")
def cp_board(kpy: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build("cp2102n_uart", 50, tmp_path_factory.mktemp("cp4l"))


def to_two_layer(pcb: Path, mode: str, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / "board.kicad_pcb"
    shutil.copyfile(pcb.with_suffix(".kicad_pro"), out.with_suffix(".kicad_pro"))
    res = sp.run_child(TO_2L, [pcb, out, mode], folder, CHILD_TIMEOUT_S)
    assert res["layers"] == 2, res
    return out


def test_live_two_layer_b_mixed_routes_both_layers(kpy: str, freerouting: tuple[str, str],
                                                   cp_board: Path, tmp_path: Path,
                                                   record_property: RecordProperty) -> None:
    pcb = to_two_layer(cp_board, "mixed", tmp_path / "cp2l")
    raw = export(kpy, pcb)
    policy = policy_of(design("cp2102n_uart", copper_layers=2))
    assert policy == {"signal_layers": ["F.Cu", "B.Cu"], "plane_nets": {}, "unused_layers": [],
                      "net_layers": {}}
    assert dl.derive_policy_from_dsn(raw)["plane_nets"] == {}      # the B.Cu pour is no plane
    res = route(kpy, freerouting, pcb, raw, policy)
    row = summary(res)
    record_property("cp2102n_2l_mixed", row)
    print("cp2102n 2L B-mixed", json.dumps(row))
    per_net = res["counts"]["per_net"]
    signal_on = {layer: sorted(n for n in per_net.get(layer, {}) if n != "GND")
                 for layer in ("F.Cu", "B.Cu")}
    assert signal_on["F.Cu"] and signal_on["B.Cu"], signal_on
    assert row["drc_errors"] == 0, row


def test_live_single_sided_routes_f_cu_only(kpy: str, freerouting: tuple[str, str],
                                            tmp_path: Path,
                                            record_property: RecordProperty) -> None:
    four = build("lm358_buffer", 30, tmp_path / "lm358")
    pcb = to_two_layer(four, "single", tmp_path / "lm358_1l")
    raw = export(kpy, pcb)
    policy = policy_of(design("lm358_buffer", copper_layers=1))
    assert policy["signal_layers"] == ["F.Cu"] and policy["unused_layers"] == ["B.Cu"]
    res = route(kpy, freerouting, pcb, raw, policy)
    row = summary(res)
    record_property("lm358_single_sided", row)
    print("lm358 single-sided", json.dumps(row))
    assert set(row["per_layer"]) == {"F.Cu"} and row["per_layer"]["F.Cu"] > 0, row
    assert row["vias"] == 0, row


def test_live_layer_rule_is_honoured_by_freerouting(freerouting: tuple[str, str],
                                                    tmp_path: Path,
                                                    record_property: RecordProperty) -> None:
    evidence = json.loads((FIX / "layer_rule_evidence.json").read_text(encoding="utf-8"))
    raw = (FIX / "six_layer.dsn.txt").read_text(encoding="utf-8")
    planes = {"GND": ["In1.Cu"], "+3V3": ["In4.Cu"]}
    nets = [ds.unquote(n.items[0].value) for n in
            ds.find_all(ds.child(ds.parse_nodes(raw)[0], "network"), "net")]
    signal = [n for n in nets if n not in planes]
    policy = {"signal_layers": ["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"], "plane_nets": planes,
              "net_layers": {n: {"primary": "F.Cu", "widths": {"F.Cu": 0.25, "In2.Cu": 0.15}}
                             for n in signal}}
    text, rep = dl.postprocess(raw, policy, layer_rule_verified=True)
    assert all(r == {"In2.Cu": 0.15} for r in rep["layer_rules"].values())
    dsn, ses = tmp_path / "layer_rule.dsn", tmp_path / "layer_rule.ses"
    dsn.write_bytes(text.encode("utf-8"))
    assert dsn_canon.canonicalize_file(dsn) == evidence["dsn_sha256"]
    routed = bar.route(freerouting[0], freerouting[1], str(dsn), str(ses), MAX_PASSES,
                       ROUTE_TIMEOUT_S)
    assert routed["ok"], routed
    routes = ds.find(ds.parse(ses.read_text(encoding="utf-8")), "routes")
    per_um = float(ds.find(routes, "resolution")[2])
    widths: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for net in ds.children(ds.find(routes, "network_out"), "net"):
        if ds.unquote(net[1]) in signal:
            for wire in ds.children(net, "wire"):
                path = ds.find(wire, "path")
                widths[path[1]][f"{float(path[2]) / per_um:g}"] += 1
    observed = {layer: dict(c) for layer, c in sorted(widths.items())}
    record_property("layer_rule_observed_um", observed)
    print("layer_rule SES widths (um):", json.dumps(observed))
    honoured = all(set(observed.get(layer, {})) == {str(w)}
                   for layer, w in evidence["expected_um"].items())
    assert honoured is dl.LAYER_RULE_VERIFIED is evidence["verified"], observed
    assert {k: set(v) for k, v in observed.items()} == {
        k: set(v) for k, v in evidence["observed_um"].items()}, observed
