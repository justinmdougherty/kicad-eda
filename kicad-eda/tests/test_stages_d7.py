"""pipeline/rp_feature_d7.py + pipeline/stages_d7.py (D7-24a, ADR D7 wave 2a): stackups, layer
roles and controlled impedance in the gated harness, through rp_hooks hooks only.

The World of tests/fixtures/pipeline_world.py fakes every KiCad call the harness makes; with
only the 'd7' feature enabled (world.use_features), the physics resolver runs for real
(physics_resolve without FD), while the canary (drc_guard.rules_compile), the SWIG layer-role
script and the fab-output check are scripted here. stages_d7's own helpers are unit-tested on
real inputs: the fab check on the KiCad 9.0.4 6L job file of tests/fixtures/fab_stackup, the
DSN policy summary on a real KiCad DSN post-processed by dsn_layers. The live tier (KiCad
9.0.4 kicad-cli for the canary; + pcbnew, Java and Freerouting for a cp2102n_uart copy driven
G1..G5) skips when those are absent."""
from __future__ import annotations

import ast
import copy
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

import decisions
import drc_guard
import dsn_layers as dl
import fab_stackup as fs
import physics_resolve as pr
import rp_context
import rp_feature_d7 as f7
import rp_hooks
import rp_route_hash
import stages_d7 as s7
import toolchain as tc
from conftest import load_example
from fixtures import pipeline_world
from fixtures.pipeline_world import DESIGN, K11_HEADER

world = pipeline_world.world            # the fixture (an assignment: see pipeline_world)

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures"
PIPELINE = HERE.parent / "pipeline"
SIX = {"preset": "jlcpcb:JLC06161H-2116", "impedance_control": {"required": True},
       "rationale": "6L: two inner routing layers"}
CANARY_OK = {"ok": True, "rc": 0, "crash": False, "fired": 1, "canary_added": False,
             "types": {"track_width": 1}}
ROLES_OK = {"ok": True, "issues": [], "warnings": [], "metrics": {},
            "stackup": {"source": "artifact", "artifact": "x", "note": None},
            "board_copper": [], "stackup_copper": [], "pcbnew": "9.0.4"}
FAB_OK = {"dir": "g", "gbrjob": {}, "ipc2581": None, "csv": "c", "notes": "n", "problems": []}


def _six(**specs) -> dict:
    """The World's two resistors on the 6L JLC06161H-2116 build, N1 a 50 ohm line on F.Cu
    and In2.Cu (In2 a dual stripline: R27 and R28 apply)."""
    nets = {**copy.deepcopy(DESIGN["nets"]), "GND": [], "+3V3": []}
    return {**copy.deepcopy(DESIGN), "nets": nets, "stackup": dict(SIX),
            "net_specs": specs or {"N1": {"impedance": 50, "layers": ["F.Cu", "In2.Cu"]}}}


def _two() -> dict:
    return {**copy.deepcopy(DESIGN), "copper_layers": 2}


def _napkin() -> dict:
    return {**copy.deepcopy(DESIGN), "mode": "napkin"}


@pytest.fixture
def d7(world, monkeypatch):
    """The World with only the d7 feature: physics without FD, a canary that compiles, a
    clean SWIG role check and a clean fab check, each call recorded on the world."""
    world.canary, world.role_runs, world.fab_checks = [], [], []
    monkeypatch.setattr(s7, "FD_MODE", "off")
    monkeypatch.setattr(drc_guard, "rules_compile", lambda text, cli, **kw: (
        world.canary.append((text, cli)) or {**CANARY_OK, "kicad_cli": cli}))
    monkeypatch.setattr(s7, "run_role_checks", lambda pcb, netlist, art: (
        world.role_runs.append((pcb, netlist, art)) or copy.deepcopy(world.roles)))
    monkeypatch.setattr(s7, "fab_stackup_check", lambda pcb, art, zip_path: (
        world.fab_checks.append((pcb, zip_path)) or copy.deepcopy(world.fab_check)))
    world.roles, world.fab_check = dict(ROLES_OK), dict(FAB_OK)
    world.use_features("d7")
    return world


def _gate(rep: dict, name: str) -> dict:
    return next(g for g in rep["gates"] if g["gate"] == name)


def _ctx(world, **kw) -> rp_context.RunContext:
    ctx = rp_context.RunContext(netlist=str(world.netlist), w=30, h=30, **kw)
    ctx.design = json.loads(world.netlist.read_text(encoding="utf-8"))
    return ctx


def _resolved(world, design: dict) -> rp_context.RunContext:
    """A context whose artifact is resolved (after_g2) for `design`."""
    world.write_design(design)
    ctx = _ctx(world)
    f7.after_g2(ctx)
    return ctx


# --- the module -----------------------------------------------------------------------------

def test_the_feature_is_discovered_with_its_order_keys_and_hooks():
    assert "d7" in rp_hooks.available() and rp_hooks.load("d7") is f7
    assert f7.ORDER == 40 and f7.ROUTE_HASH_DESIGN_KEYS == ("rf", "layer_policy")
    hooks = {name for name in rp_hooks.HOOKS if callable(getattr(f7, name, None))}
    assert hooks == {"g1_prepare", "after_g2", "route_hash_extra", "handoff_veto",
                     "g4_verdict", "emc_args", "g5_verdict"}


def _functions(path: Path) -> dict[str, int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name: n.end_lineno - min([n.lineno, *(d.lineno for d in n.decorator_list)]) + 1
            for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_the_modules_stay_small_and_only_the_hooks_module_writes_report_keys():
    for path in (PIPELINE / "stages_d7.py", PIPELINE / "rp_feature_d7.py", Path(__file__)):
        assert len(path.read_text(encoding="utf-8").splitlines()) < 800, path.name
    assert {n: k for n, k in _functions(PIPELINE / "stages_d7.py").items() if k >= 50} == {}
    assert {n: k for n, k in _functions(PIPELINE / "rp_feature_d7.py").items() if k >= 50} == {}
    assert "ctx.report" not in (PIPELINE / "stages_d7.py").read_text(encoding="utf-8")
    hooks = (PIPELINE / "rp_feature_d7.py").read_text(encoding="utf-8")
    for key in ("layer_policy", "physics", "dru", "layer_roles", "dsn_policy", "stackup"):
        assert f'"{key}"' in hooks          # docs/PIPELINE.md report keys are rp_*.py literals


def test_the_world_without_the_feature_resolves_no_physics(world):
    rep = world.run()
    assert rep["PASS"] and "physics" not in rep
    assert not (world.dir / "netlist.stackup.json").exists()


# --- a full run -------------------------------------------------------------------------------

def test_a_run_writes_the_artifact_and_rules_and_reports_every_d7_key(d7):
    rep = d7.run()
    assert rep["PASS"], rep["gate_summary"]
    art = pr.read_artifact(d7.dir / "netlist.stackup.json")
    dru = (d7.dir / "netlist.kicad_dru").read_text(encoding="utf-8")
    assert dru.count("kicad-eda managed") == 2
    phys = rep["physics"]
    assert phys["ok"] and phys["artifact"] == str(d7.dir / "netlist.stackup.json")
    assert phys["stackup"] == art["stackup"] and phys["lines"] == art["lines"] == {}
    assert phys["data_version"] == art["data_version"] == pr.data_version()
    assert rep["dru"]["ok"] and rep["dru"]["fired"] == 1
    assert rep["dru"]["path"] == str(d7.dir / "netlist.kicad_dru")
    [(text, cli)] = d7.canary
    assert text == dru and cli == tc.board_cli()
    assert d7.names().index("build") > d7.names().index("schematic")      # physics before G3
    assert rep["layer_roles"]["ok"] and len(d7.role_runs) == 1
    assert d7.role_runs[0] == (str(d7.pcb), str(d7.netlist), str(d7.dir / "netlist.stackup.json"))
    assert rep["dsn_policy"]["plane_nets"] == art["dsn"]["plane_nets"]
    assert rep["fab"]["stackup"] == FAB_OK and d7.fab_checks
    assert rep["layer_policy"]["applied"] is False


def test_physics_errors_halt_g2_before_the_board_is_built(d7, monkeypatch):
    monkeypatch.setattr(pr, "write_artifact", lambda *a, **kw: {
        "ok": False, "path": "p", "errors": ["ST-E9 at $.net_specs.N1: no reference"],
        "warnings": []})
    rep = d7.run()
    g2 = _gate(rep, "G2")
    assert not rep["PASS"] and not g2["passed"] and "build" not in d7.names()
    assert any("ST-E9" in b and "ADR D7.4" in b for b in g2["blockers"])
    assert rep["feature_halt"]["feature"] == "d7" and rep["feature_halt"]["hook"] == "after_g2"
    assert rep["physics"]["errors"] and not d7.canary


def test_an_artifact_that_cannot_be_written_halts_g2(d7, monkeypatch):
    def boom(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(pr, "write_artifact", boom)
    rep = d7.run()
    assert any("disk full" in b for b in _gate(rep, "G2")["blockers"])
    assert "build" not in d7.names() and rep["physics"]["ok"] is False


def test_rules_that_do_not_compile_halt_g2_r23(d7, monkeypatch):
    monkeypatch.setattr(drc_guard, "rules_compile", lambda text, cli, **kw: {
        "ok": False, "rc": 0, "crash": False, "fired": 0, "canary_added": True, "types": {},
        "kicad_cli": cli, "reason": "kicad-cli exited 0 but the canary rule did not fire"})
    rep = d7.run()
    g2 = _gate(rep, "G2")
    assert not g2["passed"] and any("canary rule did not fire" in b for b in g2["blockers"])
    assert rep["dru"]["ok"] is False and "build" not in d7.names()


def test_compile_rules_checks_the_board_file_with_every_board_cli(tmp_path, monkeypatch):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(K11_HEADER, encoding="utf-8")
    (tmp_path / "b.kicad_dru").write_text("(version 1)\n", encoding="utf-8")
    seen = []
    monkeypatch.setattr(tc, "board_cli", lambda p="": "k11-cli" if p else "k9-cli")
    monkeypatch.setattr(drc_guard, "rules_compile", lambda text, cli, **kw: (
        seen.append((text, cli)) or {**CANARY_OK, "kicad_cli": cli, "ok": cli == "k9-cli",
                                      **({} if cli == "k9-cli" else {"reason": "rc 3"})}))
    res = s7.compile_rules(str(pcb))
    assert seen == [("(version 1)\n", "k9-cli"), ("(version 1)\n", "k11-cli")]
    assert res["ok"] is False and res["kicad_cli"] == "k9-cli" and "rc 3" in res["reason"]
    assert [o["kicad_cli"] for o in res["others"]] == ["k11-cli"]
    missing = s7.compile_rules(str(tmp_path / "none.kicad_pcb"))
    assert missing["ok"] is False and "none.kicad_dru" in missing["reason"]


# --- G1: the napkin layer default -----------------------------------------------------------

def test_a_napkin_netlist_without_a_count_gets_the_d711_default_once(d7):
    d7.write_design(_napkin())
    rep = d7.run(napkin=True)
    assert rep["PASS"], rep["gate_summary"]
    written = json.loads(d7.netlist.read_text(encoding="utf-8"))
    assert written["copper_layers"] == 4 and written["mode"] == "napkin"
    assert written["stackup"]["preset"] == "jlcpcb:JLC04161H-7628"
    assert written["stackup"]["origin"] == "layer_policy" and written["stackup"]["rationale"]
    assert {k: v for k, v in written.items() if k not in ("copper_layers", "stackup")} == _napkin()
    assert [r["gate"] for r in rep["remediation"]][:1] == ["G1"]
    entries = [e for e in decisions.load_decisions(str(d7.netlist)).entries
               if e["stage"] == "requirements"]
    assert len(entries) == 1 and entries[0]["override"] == "stackup.preset"
    assert entries[0]["confidence"] == "med"
    assert entries[0]["decision"] == "4L jlcpcb:JLC04161H-7628"
    pol = rep["layer_policy"]
    assert pol["applied"] and pol["decision"] == entries[0]["id"] and pol["copper_layers"] == 4
    assert pr.read_artifact(d7.dir / "netlist.stackup.json")["stackup"]["source"] == "policy"
    assert not list(d7.dir.glob("*.tmp"))                           # atomic: no temp file left
    before = d7.netlist.read_bytes()
    again = d7.run(napkin=True)
    assert d7.netlist.read_bytes() == before and again["layer_policy"]["applied"] is False
    assert len([e for e in decisions.load_decisions(str(d7.netlist)).entries
                if e["stage"] == "requirements"]) == 1


def test_a_napkin_netlist_gets_the_default_even_with_strict(d7):
    d7.write_design(_napkin())
    assert d7.run(napkin=False)["layer_policy"]["applied"]   # "mode": "napkin" always wins


@pytest.mark.parametrize("design,word", [(DESIGN, '"mode": "napkin"'),
                                         ({**_two(), "mode": "napkin"}, "declares")],
                         ids=["harness-default-napkin", "count-declared"])
def test_no_layer_default_for_a_netlist_that_is_not_napkin_or_declares_a_count(d7, design, word):
    d7.write_design(copy.deepcopy(design))
    before = d7.netlist.read_bytes()
    rep = d7.run(napkin=True)
    assert d7.netlist.read_bytes() == before and rep["layer_policy"]["applied"] is False
    assert word in rep["layer_policy"]["reason"]
    assert "G1" not in [r["gate"] for r in rep.get("remediation", [])]


def test_apply_layer_default_returns_a_new_design_and_never_mutates(tmp_path):
    design = _napkin()
    keep = copy.deepcopy(design)
    path = tmp_path / "n.json"
    path.write_text(json.dumps(design), encoding="utf-8")
    new, nd = s7.apply_layer_default(str(path), design)
    assert design == keep and new is not design and new["copper_layers"] == nd.copper_layers
    assert json.loads(path.read_text(encoding="utf-8")) == new
    assert s7.apply_layer_default(str(path), new) is None


# --- route hash -------------------------------------------------------------------------------

@pytest.fixture
def use(monkeypatch):
    monkeypatch.setattr(rp_hooks, "_override", rp_hooks.selection())
    return lambda *features: rp_hooks.set_features(list(features))


def test_route_hash_extra_is_the_physics_data_version(monkeypatch, use):
    assert f7.route_hash_extra(DESIGN, None) == {"physics_data_version": pr.data_version()}
    use(f7)
    first = rp_route_hash.route_hash(DESIGN)
    monkeypatch.setattr(pr, "data_version", lambda *a, **kw: "0" * 64)
    assert rp_route_hash.route_hash(DESIGN) != first


@pytest.mark.parametrize("key,value", [("rf", {"chains": []}), ("layer_policy", {"fab": "pcbway"})])
def test_rf_and_layer_policy_move_the_route_hash_only_with_d7(use, key, value):
    changed = {**copy.deepcopy(DESIGN), key: value}
    use()
    assert rp_route_hash.route_hash(changed) == rp_route_hash.route_hash(DESIGN)
    use(f7)
    assert rp_route_hash.route_hash(changed) != rp_route_hash.route_hash(DESIGN)
    assert key in rp_route_hash.registry().design_keys


def test_clock_hz_never_moves_the_route_hash(use):
    use(f7)
    clocked = copy.deepcopy(DESIGN)
    clocked["components"][0]["clock_hz"] = [4.8e7, 32768]
    assert rp_route_hash.route_hash(clocked) == rp_route_hash.route_hash(DESIGN)
    assert "clock_hz" not in rp_route_hash.registry().component_keys
    assert pr.design_digest(clocked) != pr.design_digest(DESIGN)     # the artifact still sees it


# --- handoff_veto -----------------------------------------------------------------------------

def test_handoff_veto_refuses_a_6l_impedance_design_and_allows_a_plain_2l_one(d7):
    ctx = _resolved(d7, _six())
    reason = f7.handoff_veto(ctx)
    assert reason.startswith(s7.VETO_REASON)
    assert "N1" in reason and "R27" in reason and "R28" in reason
    assert f7.handoff_veto(_resolved(d7, _two())) is None


def test_handoff_veto_reads_the_artifact_on_disk_or_the_design(d7):
    _resolved(d7, _six())
    fresh = _ctx(d7)                                  # no ctx.ext: the artifact on disk
    assert f7.handoff_veto(fresh).startswith(s7.VETO_REASON)
    (d7.dir / "netlist.stackup.json").unlink()
    assert "net_specs" in f7.handoff_veto(_ctx(d7))   # no artifact: the design's targets
    rf = {**_two(), "rf": {"antennas": [{"ref": "AE1"}]}}
    assert "RF" in s7.veto(None, rf) and s7.veto(None, _two()) is None


def test_an_auto_run_never_hands_an_impedance_board_to_the_agent(d7):
    d7.write_design(_six())
    d7.routes = [{"ok": False, "tracks": 3, "unconnected": 2}]
    d7.drcs = [{**pipeline_world.CLEAN_DRC, "unconnected": 2}]
    rep = d7.run(route="auto")
    g4 = _gate(rep, "G4")
    assert rep.get("status") != "handoff" and not g4["passed"]
    assert any(b.startswith("routing handoff refused: " + s7.VETO_REASON) for b in g4["blockers"])


# --- G4: the layer-role checks ----------------------------------------------------------------

def test_r8_issues_block_g4_and_r27_r28_warn(d7):
    d7.roles = {**ROLES_OK, "issues": ["R8: In1.Cu is the GND plane but carries 1 foreign track"],
                "warnings": ["R27: N1 changes layer F.Cu -> In2.Cu"]}
    rep = d7.run()
    g4 = _gate(rep, "G4")
    assert not g4["passed"] and d7.roles["issues"][0] in g4["blockers"]
    assert "R27: N1 changes layer F.Cu -> In2.Cu" in g4["warnings"]
    assert rep["layer_roles"]["issues"] == d7.roles["issues"]


def test_role_checks_that_cannot_run_block_g4_with_the_reason(d7):
    d7.roles = {"ok": False, "error": "pcbnew is not importable"}
    rep = d7.run()
    assert any(s7.NOT_JUDGED in b and "pcbnew is not importable" in b
               for b in _gate(rep, "G4")["blockers"])


def test_one_board_is_checked_once_however_often_g4_is_judged(d7):
    d7.write_design({**copy.deepcopy(DESIGN), "emc": {"stitch": True, "max_freq_hz": 1e8}})
    rep = d7.run()
    assert rep["PASS"] and len(d7.role_runs) == 2          # routed board, then the stitched one
    assert d7.emc_extra == [[f"--stackup={d7.dir / 'netlist.stackup.json'}"]]
    d7.role_runs.clear()
    ctx = _resolved(d7, DESIGN)
    verdict = {"passed": True, "blockers": [], "warnings": []}
    f7.g4_verdict(ctx, verdict)
    f7.g4_verdict(ctx, verdict)
    assert len(d7.role_runs) == 1


def test_a_2l_board_without_planes_or_lines_is_not_checked(d7):
    d7.write_design(_two())
    rep = d7.run()
    assert rep["PASS"] and not d7.role_runs and "nothing to check" in rep["layer_roles"]["skipped"]


@pytest.mark.parametrize("design,blocked", [(_six(), True), (DESIGN, False)], ids=["6L", "4L"])
def test_a_resumed_kicad_11_board_gets_the_blocker_not_an_exception(d7, design, blocked):
    ctx = _resolved(d7, design)
    ctx.resume = rp_context.RESUME_ROUTED
    d7.pcb.write_text(K11_HEADER, encoding="utf-8")
    out = f7.g4_verdict(ctx, {"passed": True, "blockers": [], "warnings": []})
    assert not d7.role_runs and ctx.report["layer_roles"]["judged"] is False
    if blocked:
        assert out["passed"] is False and out["blockers"][0].startswith(s7.K11_BLOCKER)
    else:
        assert out["passed"] and any(s7.K11_NOTE in w for w in out["warnings"])


def test_without_an_artifact_g4_and_g5_block_with_the_reason_never_skip(d7):
    ctx = _ctx(d7)                       # stage_physics never ran: no artifact anywhere
    d7.pcb.write_text("(kicad_pcb (version 20241229))\n", encoding="utf-8")
    passed = {"passed": True, "blockers": [], "warnings": []}
    g4 = f7.g4_verdict(ctx, passed)
    assert not g4["passed"] and s7.NO_ARTIFACT in g4["blockers"][0] and not d7.role_runs
    g5 = f7.g5_verdict(ctx, passed)
    assert not g5["passed"] and s7.NO_ARTIFACT in g5["blockers"][0] and not d7.fab_checks
    assert ctx.report["fab"]["stackup"]["checked"] is False


def test_run_role_checks_reads_the_scripts_last_json_line(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(tc, "kicad_python", lambda: "kpy")

    def fake(argv, **kw):
        calls.append((argv, kw))
        out = 'wx noise\n{"ok": true, "issues": ["R8: x"], "warnings": []}\n'
        return subprocess.CompletedProcess(argv, 0, out, "")
    monkeypatch.setattr(tc, "run", fake)
    res = s7.run_role_checks("b.kicad_pcb", "n.json", "n.stackup.json")
    assert res["ok"] and res["issues"] == ["R8: x"]
    assert calls[0][0] == ["kpy", str(s7.ROLE_CHECK_SCRIPT), "b.kicad_pcb", "n.json",
                           "n.stackup.json"] and calls[0][1]["timeout"] == s7.ROLE_CHECK_TIMEOUT_S
    monkeypatch.setattr(tc, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 3, "", "crash"))
    assert "exit 3" in s7.run_role_checks("b", "n", None)["error"]
    monkeypatch.setattr(tc, "kicad_python", lambda: None)
    assert s7.run_role_checks("b", "n", None)["ok"] is False


# --- the DSN policy summary ---------------------------------------------------------------------

POLICY = {"signal_layers": ["F.Cu", "B.Cu"], "plane_nets": {"GND": ["In1.Cu"], "+3V3": ["In2.Cu"]},
          "unused_layers": [], "net_layers": {"RXD": {"primary": "F.Cu", "window": [0.19, 0.21],
                                                      "widths": {"F.Cu": 0.2, "B.Cu": 0.3}}}}
ART4 = {"stackup": {"copper": [{"name": n} for n in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")]},
        "dsn": POLICY}


def _dsn(tmp_path: Path, policy: dict | None, variant: str | None = None) -> Path:
    raw = (FIX / "dsn_layers" / "cp2102n.dsn.txt").read_text(encoding="utf-8")
    text = raw if policy is None else dl.postprocess(raw, policy, variant=variant)[0]
    path = tmp_path / "b.dsn"
    path.write_text(text, encoding="utf-8")
    return path


def test_dsn_policy_reports_the_routed_layers_and_the_applied_classes(tmp_path):
    rep, warnings = s7.dsn_policy_report(ART4, "a.json", _dsn(tmp_path, POLICY))
    _text, real = dl.postprocess(_dsn(tmp_path, None).read_text(encoding="utf-8"), POLICY)
    for key in ("routed_layers", "layer_rules", "excluded_layers"):     # what dsn_layers says
        assert rep[key] == real[key], key
    # B.Cu's 0.3 mm lies outside F.Cu's window: with the verified per-class layer_rule it
    # joins at its own width, else it is excluded (ADR D7.9)
    verified = dl.LAYER_RULE_VERIFIED
    assert rep["routed_layers"] == {"RXD": ["F.Cu", "B.Cu"] if verified else ["F.Cu"]}
    assert rep["dsn"]["applied"] and rep["dsn"]["variant"] == "restricted" and warnings == []
    unrestricted, _ = s7.dsn_policy_report(ART4, "a.json", _dsn(tmp_path, POLICY, "unrestricted"))
    assert unrestricted["dsn"]["variant"] == "unrestricted"


def test_dsn_policy_warns_when_the_routed_dsn_carries_no_policy(tmp_path):
    rep, warnings = s7.dsn_policy_report(ART4, "a.json", _dsn(tmp_path, None))
    assert rep["dsn"]["applied"] is False and len(warnings) == 1 and "R8" in warnings[0]
    gone, none = s7.dsn_policy_report(ART4, "a.json", tmp_path / "missing.dsn")
    assert gone["dsn"]["found"] is False and none == []
    other = {**POLICY, "net_layers": {"RXD": {"primary": "F.Cu", "widths": {"F.Cu": 0.2}}}}
    _, mismatch = s7.dsn_policy_report({**ART4, "dsn": other}, "a.json", _dsn(tmp_path, POLICY))
    assert len(mismatch) == 1 and "RXD" in mismatch[0]
    assert s7.dsn_policy_report(None, None, tmp_path / "x.dsn")[0]["applied"] is False


# --- G5: the fab outputs that carry the stackup ------------------------------------------------

@pytest.fixture
def six_fab(tmp_path):
    """s6L.kicad_pcb with its KiCad 9.0.4 Gerbers + job file in s6L_gerbers/ (fixed), the
    stackup CSV, fab notes and an IPC-2581 file, all zipped as the fab export would."""
    design = json.loads((FIX / "fab_stackup" / "s6l_k9.json").read_text(encoding="utf-8"))
    art = pr.resolve_design(design["design"], tmp_path / "s6L.json", fd="off")
    out = tmp_path / "s6L_gerbers"
    shutil.copytree(FIX / "fab_stackup" / "k9_6l", out)
    fs.fix_gbrjob_file_attributes(out / "s6L-job.gbrjob")
    fs.write_outputs(out, "s6L", art)
    (out / "s6L_ipc2581.xml").write_text("<IPC-2581/>", encoding="utf-8")
    zpath = tmp_path / "s6L_JLCPCB.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        for f in out.iterdir():
            z.write(f, arcname=f.name)
    return tmp_path / "s6L.kicad_pcb", art, zpath


def test_fab_check_passes_complete_outputs(six_fab):
    pcb, art, zpath = six_fab
    res = s7.fab_stackup_check(str(pcb), art, str(zpath))
    assert res["problems"] == [] and res["gbrjob"]["rows"] == res["gbrjob"]["expected_rows"] == 14
    assert res["ipc2581"]["reasons"] and res["csv"].endswith("s6L_stackup.csv")


def test_fab_check_problems_become_g5_blockers(six_fab, tmp_path):
    pcb, art, zpath = six_fab
    shutil.copy(FIX / "fab_stackup" / "k9_6l" / "s6L-job.gbrjob", tmp_path / "s6L_gerbers")
    (tmp_path / "s6L_gerbers" / "s6L_ipc2581.xml").unlink()
    res = s7.fab_stackup_check(str(pcb), art, str(zpath))
    assert sum("fix_gbrjob_file_attributes" in p for p in res["problems"]) == 4
    assert any("s6L_ipc2581.xml" in p and "missing" in p for p in res["problems"])
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("readme.txt", "")
    assert any("not in" in p for p in s7.fab_stackup_check(str(pcb), art, str(zpath))["problems"])


def test_g5_turns_fab_problems_into_blockers(d7):
    d7.fab_check = {**FAB_OK,
                    "problems": ["job file s6L-job.gbrjob: 3 thickness rows, expected 14"]}
    rep = d7.run()
    g5 = _gate(rep, "G5")
    assert not g5["passed"] and any("3 thickness rows" in b and "R24" in b for b in g5["blockers"])
    assert rep["fab"]["stackup"]["problems"] and d7.fab_checks[0] == (
        str(d7.pcb), str(d7.dir / "netlist_JLCPCB.zip"))


# --- live ---------------------------------------------------------------------------------------

def _real_cli() -> str:
    cli = tc.board_cli()
    if not cli or not Path(cli).is_file():
        pytest.skip("no kicad-cli installed")
    return cli


def test_live_the_canary_compiles_the_managed_rules_of_a_6l_impedance_board(tmp_path):
    _real_cli()
    netlist = tmp_path / "n.json"
    netlist.write_text(json.dumps(_six()), encoding="utf-8")
    written = pr.write_artifact(str(netlist), fd="off")
    assert written["ok"], written["errors"]
    res = s7.compile_rules(str(tmp_path / "n.kicad_pcb"))
    assert res["ok"] and res["fired"] >= 1, res
    (tmp_path / "n.kicad_dru").write_text("(version 1)\n(rule bad (constraint track_width "
                                          "(min 1mm)) (layer Bogus.Cu))\n", encoding="utf-8")
    assert s7.compile_rules(str(tmp_path / "n.kicad_pcb"))["ok"] is False


def test_live_cp2102n_uart_passes_g1_to_g5_with_the_d7_outputs(tmp_path, monkeypatch):
    _real_cli()
    if not tc.kicad_python() or not tc.find_java() or not tc.freerouting_jar(""):
        pytest.skip("KiCad's pcbnew Python, Java or the Freerouting jar is missing")
    import run_all
    import run_pipeline as rp
    monkeypatch.setattr(rp_hooks, "_override", rp_hooks.selection())
    rp_hooks.set_features(None)                      # every discovered feature, as a real run
    netlist = tmp_path / "cp" / "netlist.json"
    netlist.parent.mkdir()
    # J1.SH is no pad of KiCad 9.0.4's stock USB-C footprint (tests/test_board_pipeline_d7_live
    # drops it too: the footprint names its shield per release): left unconnected here
    design = load_example("cp2102n_uart")
    design["nets"] = {n: [p for p in conns if p != ["J1", "SH"]]
                      for n, conns in design["nets"].items()}
    design["no_connect"] = [*design.get("no_connect", []), ["J1", "SH"]]
    netlist.write_text(json.dumps(design, indent=2), encoding="utf-8")
    rep = rp.run(str(netlist), 50, 50, route=run_all.ROUTE)
    assert rep["PASS"], rep["gate_summary"]
    stem = netlist.with_suffix("")
    assert pr.read_artifact(f"{stem}.stackup.json") and Path(f"{stem}.kicad_dru").is_file()
    assert rep["dru"]["ok"] and rep["layer_roles"]["ok"] and rep["layer_roles"]["issues"] == []
    assert rep["fab"]["stackup"]["problems"] == []
    names = zipfile.ZipFile(Path(stem.parent, rep["fab"]["zip"])).namelist()
    assert {"netlist_ipc2581.xml", "netlist_stackup.csv", "netlist_fab_notes.md"} <= set(names)
    assert rep["physics"]["stackup"]["copper_layers"] == 4 and rep["dsn_policy"]["dsn"]["applied"]
