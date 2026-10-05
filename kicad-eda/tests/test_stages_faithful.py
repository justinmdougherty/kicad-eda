"""pipeline/stages_faithful.py: the faithful-rebuild pipeline I/O (D4).

The stage functions take a duck-typed RunContext (a SimpleNamespace here,
plus one check against the real rp_context.RunContext). Boards are the
net_equivalence fixtures (tests/fixtures/equiv/board_k9.sexpr, the
tests/fixtures/faithful/mixed_board.sexpr DRC board) copied to tmp as
.kicad_pcb, with their kicad-cli DRC JSONs; references are frozen with
net_equivalence.write_reference. Verdicts come from the real gates functions.
The live test (skips without kicad-cli) exports the generated schematic of
examples/lm358_buffer for the G2 path.
"""
from __future__ import annotations

import builtins
import copy
import json
import pathlib
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import faithful as fa
import gates
import import_review as ir
import net_equivalence as ne
import stages_faithful as sf

TESTS = Path(__file__).resolve().parent
EQUIV = TESTS / "fixtures" / "equiv"
FAITH = TESTS / "fixtures" / "faithful"
LM358 = TESTS.parent / "examples" / "lm358_buffer" / "netlist.json"
SCALES = (1.0, 1.5, 2.0)
CONNECTOR = "J1: mating face points INTO the board interior (unusable in an enclosure)"
EMPTY_DRC = {"violations": [], "unconnected_items": []}
REVIEW = {"decoupling": {"uncovered": []}, "flags": {}}


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def _equiv_design(**over: Any) -> dict:
    design = json.loads((EQUIV / "netlist.json").read_text(encoding="utf-8"))
    source = {"kind": "kicad", "path": "src/equiv.kicad_pro",
              "reference": "reference_padnets.json"}
    return {**design, "source": source, "rebuild": "keep", **over}


def _ctx(tmp: Path, design: dict, *, board: Path | None = EQUIV / "board_k9.sexpr",
         drc: dict | Path | None = None) -> SimpleNamespace:
    """A RunContext look-alike for <tmp>/rb.json with the board copied in."""
    netlist = tmp / "rb.json"
    netlist.write_text(json.dumps(design), encoding="utf-8")
    base = str(tmp / "rb")
    if board is not None:
        shutil.copy(board, base + ".kicad_pcb")
    if isinstance(drc, Path):
        shutil.copy(drc, base + "_drc.json")
    elif drc is not None:
        Path(base + "_drc.json").write_text(json.dumps(drc), encoding="utf-8")
    return SimpleNamespace(netlist=str(netlist), design=design, base=base,
                           sch=base + ".kicad_sch", pcb=base + ".kicad_pcb",
                           report={"netlist": str(netlist), "gates": []},
                           board_rep={"ok": True})


def _board_nets(board: Path = EQUIV / "board_k9.sexpr") -> dict:
    return ne.pcb_pad_nets(board.read_text(encoding="utf-8"))


def _freeze(tmp: Path, nets: dict, name: str = "reference_padnets.json", **kw: Any) -> Path:
    return ne.write_reference(tmp / name, nets, **kw)


def _move_r2_pad2_to_vcc(pcb: str) -> None:
    """R2 pad 2 GND -> VCC on a copied board_k9 (GND split, VCC merged)."""
    path = Path(pcb)
    text = path.read_text(encoding="utf-8")
    at = text.index('(property "Reference" "R2"')
    pad = text.index('(pad "2"', at)
    net = text.index('(net 3 "GND")', pad)
    assert net < text.index("(footprint", pad)            # still inside R2
    path.write_text(text[:net] + '(net 1 "VCC")' + text[net + len('(net 3 "GND")'):],
                    encoding="utf-8")


def _loaded(tmp: Path, design: dict | None = None, **kw: Any) -> SimpleNamespace:
    """A context whose reference is board_k9 frozen, loaded."""
    _freeze(tmp, _board_nets(), board_only=["H1"])
    ctx = _ctx(tmp, design or _equiv_design(), **kw)
    sf.load(ctx)
    return ctx


def _xml_export(fixture: Path = EQUIV / "sch_k9.xml", calls: list | None = None) -> Any:
    def export(sch: Any, out: Any, cli: Any, **_kw: Any) -> Path:
        if calls is not None:
            calls.append((Path(sch), Path(out), cli))
        Path(out).write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
        return Path(out)
    return export


def _fake_schematic(monkeypatch: pytest.MonkeyPatch, ctx: SimpleNamespace,
                    fixture: Path = EQUIV / "sch_k9.xml", calls: list | None = None) -> None:
    Path(ctx.sch).write_text("(kicad_sch)", encoding="utf-8")
    monkeypatch.setattr(sf.tc, "kicad_cli", lambda: "kicad-cli")
    monkeypatch.setattr(sf.ne, "export_schematic_xml", _xml_export(fixture, calls))


def _g2_verdict(erc: int | None = 0, rails: tuple[str, ...] = ()) -> dict:
    review = {**REVIEW, "decoupling": {"uncovered": [{"ref": "U1", "rails_without_cap":
                                                       list(rails)}] if rails else []}}
    return gates.g2_schematic({"ok": True, "erc_violations": erc}, review, None)


def _record(ctx: SimpleNamespace) -> dict:
    return ctx.report["faithful"]


# ----------------------------------------------------------------------------
# inactive: identity, no file touched
# ----------------------------------------------------------------------------
def _forbid_files(m: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("an inactive design touched the file system")

    for name in ("read_text", "write_text", "read_bytes", "write_bytes", "is_file", "exists",
                 "mkdir", "unlink", "stat", "open"):
        m.setattr(pathlib.Path, name, boom)
    m.setattr(builtins, "open", boom)
    m.setattr(subprocess, "run", boom)
    m.setattr(tempfile, "TemporaryDirectory", boom)
    for name in ("load_pad_nets_meta", "export_schematic_xml", "pcb_pad_nets"):
        m.setattr(sf.ne, name, boom)
    m.setattr(sf.tc, "kicad_cli", boom)


@pytest.mark.parametrize("source", ["absent", None])
def test_inactive_every_function_returns_its_inputs_and_touches_no_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: Any) -> None:
    design = json.loads((EQUIV / "netlist.json").read_text(encoding="utf-8"))
    if source is None:
        design = {**design, "source": None}
    ctx = _ctx(tmp_path, design, drc=FAITH / "drc_mixed.json")
    before = sorted(p.name for p in tmp_path.iterdir())
    snapshot = copy.deepcopy((ctx.report, ctx.design))
    verdict, cy, pl = _g2_verdict(3, ("VCC",)), {"courtyard_violations": 2}, {
        "issues": [CONNECTOR], "warnings": []}
    drc, routed = {"errors": 4, "unconnected": 1, "types": {"clearance": 4}}, {
        "issues": ["w"], "warnings": []}
    with monkeypatch.context() as m:
        _forbid_files(m)
        sf.load(ctx)
        assert sf.active(ctx) is False
        assert sf.pack_scales(ctx, SCALES) is SCALES
        assert sf.g2_judge(ctx, verdict) is verdict
        got_cy, got_pl = sf.g3_inputs(ctx, cy, pl)
        got_drc, got_routed = sf.g4_inputs(ctx, drc, routed)
    assert got_cy is cy and got_pl is pl and got_drc is drc and got_routed is routed
    assert (ctx.report, ctx.design) == snapshot and "faithful" not in ctx.report
    assert verdict == _g2_verdict(3, ("VCC",)) and pl == {"issues": [CONNECTOR], "warnings": []}
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_a_context_that_was_never_loaded_is_inactive() -> None:
    ctx = SimpleNamespace(design={"components": []}, report={}, netlist="x.json")
    verdict = _g2_verdict(1)
    assert not sf.active(ctx) and sf.g2_judge(ctx, verdict) is verdict
    assert sf.state(ctx) is sf.INACTIVE_RUN and ctx.report == {}


# ----------------------------------------------------------------------------
# load / active / pack_scales
# ----------------------------------------------------------------------------
def test_load_records_the_profile_and_reads_the_reference(tmp_path: Path) -> None:
    (tmp_path / "import_report.json").write_text("{}", encoding="utf-8")
    design = _equiv_design()
    original = copy.deepcopy(design)
    ctx = _loaded(tmp_path, design)
    rec = _record(ctx)
    assert sf.active(ctx) and ctx.design == original
    assert {k: rec[k] for k in ("active", "kind", "rebuild")} == {
        "active": True, "kind": "kicad", "rebuild": "keep"}
    assert rec["reference"] == str(tmp_path / "reference_padnets.json")
    assert rec["import_report"] == str(tmp_path / "import_report.json")
    assert rec["equivalence"] == {} and rec["relaxed"] == [] and "error" not in rec
    run = sf.state(ctx)
    assert run.profile == fa.profile(design, ctx.netlist) and run.error is None
    assert run.reference[1]["kind"] == "reference" and run.reference[1]["board_only_refs"] == [
        "H1"]


def test_load_resolves_an_explicit_import_report_and_omits_a_missing_default(
        tmp_path: Path) -> None:
    source = {**_equiv_design()["source"], "import_report": "${KIPRJMOD}/imp/rep.json"}
    ctx = _loaded(tmp_path, _equiv_design(source=source))
    assert Path(_record(ctx)["import_report"]) == tmp_path / "imp" / "rep.json"
    ctx = _loaded(tmp_path, _equiv_design())
    assert _record(ctx)["import_report"] is None


def test_the_reference_is_read_once_per_run(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[Path] = []
    real = ne.load_pad_nets_meta
    monkeypatch.setattr(sf.ne, "load_pad_nets_meta",
                        lambda p, **kw: reads.append(Path(p)) or real(p, **kw))
    ctx = _loaded(tmp_path, drc=EMPTY_DRC)
    _fake_schematic(monkeypatch, ctx)
    sf.g2_judge(ctx, _g2_verdict())
    sf.g3_inputs(ctx, {"courtyard_violations": 0}, {"issues": [], "warnings": []})
    sf.g4_inputs(ctx, {"errors": 0, "unconnected": 0, "types": {}},
                 {"issues": [], "warnings": []})
    assert reads.count(tmp_path / "reference_padnets.json") == 1
    assert reads.count(Path(ctx.pcb)) == 2                      # G3 and G4 boards


def test_pack_scales_follow_the_profile(tmp_path: Path) -> None:
    fixed = [{**c, "at": [1, 2, 0]} for c in _equiv_design()["components"]]
    ctx = _loaded(tmp_path, _equiv_design(components=fixed))
    assert sf.pack_scales(ctx, SCALES) == (1.0,)
    ctx = _loaded(tmp_path, _equiv_design(components=fixed, rebuild="reflow"))
    assert sf.pack_scales(ctx, SCALES) is SCALES
    ctx = _loaded(tmp_path)                                 # keep, parts not fixed
    assert sf.pack_scales(ctx, SCALES) is SCALES


def test_a_missing_reference_blocks_every_gate_without_crashing(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _equiv_design(), drc=EMPTY_DRC)     # no reference file written
    sf.load(ctx)
    assert sf.active(ctx) and "reference_padnets.json" in _record(ctx)["error"]
    g2 = sf.g2_judge(ctx, _g2_verdict())
    assert not g2["passed"] and "cannot be judged" in g2["blockers"][-1]
    assert g2["tasks"][-1]["auto"] is False
    _, pl = sf.g3_inputs(ctx, {"courtyard_violations": 0}, {"issues": [], "warnings": []})
    _, routed = sf.g4_inputs(ctx, {"errors": 0, "unconnected": 0, "types": {}},
                             {"issues": [], "warnings": []})
    assert pl["issues"][0].startswith("G3: ") and routed["issues"][0].startswith("G4: ")
    assert _record(ctx)["equivalence"]["G3"]["equal"] is None


def test_a_malformed_source_is_active_and_blocks(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _equiv_design(rebuild="sideways"))
    sf.load(ctx)
    rec = _record(ctx)
    assert sf.active(ctx) and rec["active"] is True and "rebuild" in rec["error"]
    assert rec["kind"] == "kicad" and sf.pack_scales(ctx, SCALES) is SCALES
    assert not sf.g2_judge(ctx, _g2_verdict())["passed"]


def test_load_also_runs_lazily_for_a_source_design(tmp_path: Path) -> None:
    _freeze(tmp_path, _board_nets(), board_only=["H1"])
    ctx = _ctx(tmp_path, _equiv_design())
    assert sf.active(ctx) and _record(ctx)["active"] is True


def test_the_real_run_context_carries_the_state(tmp_path: Path) -> None:
    import rp_context

    _freeze(tmp_path, _board_nets(), board_only=["H1"])
    netlist = tmp_path / "rb.json"
    netlist.write_text(json.dumps(_equiv_design()), encoding="utf-8")
    ctx = rp_context.RunContext(netlist=str(netlist), w=30, h=20)
    ctx.design = json.loads(netlist.read_text(encoding="utf-8"))
    sf.load(ctx)
    assert sf.active(ctx) and ctx.report["faithful"]["kind"] == "kicad"


# ----------------------------------------------------------------------------
# G2
# ----------------------------------------------------------------------------
def test_g2_relaxes_decoupling_and_judges_erc_against_the_baseline(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shutil.copy(FAITH / "erc_baseline_k9.json", tmp_path / "source_erc.json")
    source = {**_equiv_design()["source"], "erc_baseline": "source_erc.json"}
    ctx = _loaded(tmp_path, _equiv_design(source=source))
    shutil.copy(FAITH / "erc_baseline_k9.json", ctx.base + "_erc.json")
    total = sum(fa.erc_keys(FAITH / "erc_baseline_k9.json").values())
    ctx.report["schematic"] = {"ok": True, "erc_violations": total}
    _fake_schematic(monkeypatch, ctx)
    verdict = _g2_verdict(total, ("VCC",))
    out = sf.g2_judge(ctx, verdict)
    assert out["passed"] and out is not verdict and verdict == _g2_verdict(total, ("VCC",))
    assert not any(t["fix"].get("type") == "add_decoupling" for t in out["tasks"])
    rec = _record(ctx)
    assert rec["erc"] == {"total": total, "new": [], "baseline_total": total,
                          "baseline": str(tmp_path / "source_erc.json")}
    assert set(rec["equivalence"]["G2"]) == {"design", "schematic"}
    assert all(e["equal"] for e in rec["equivalence"]["G2"].values())
    assert rec["relaxed"] == [
        "G2: faithful: the source design has no bypass on VCC at U1 (kept as designed)",
        f"G2: faithful: ERC reports {total} violation(s), none new against the source baseline"]


def test_g2_new_erc_violations_stay_blockers(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    shutil.copy(FAITH / "erc_baseline_k9.json", tmp_path / "source_erc.json")
    source = {**_equiv_design()["source"], "erc_baseline": "source_erc.json"}
    ctx = _loaded(tmp_path, _equiv_design(source=source))
    shutil.copy(FAITH / "erc_rebuilt_k9.json", ctx.base + "_erc.json")
    new = fa.erc_new(FAITH / "erc_rebuilt_k9.json", FAITH / "erc_baseline_k9.json")
    total = sum(fa.erc_keys(FAITH / "erc_rebuilt_k9.json").values())
    ctx.report["schematic"] = {"ok": True, "erc_violations": total}
    _fake_schematic(monkeypatch, ctx)
    out = sf.g2_judge(ctx, _g2_verdict(total))
    assert new and not out["passed"]
    assert out["blockers"] == [f"ERC: {len(new)} violation(s) not in the source baseline: "
                               + "; ".join(new)]
    assert _record(ctx)["erc"]["new"] == new and _record(ctx)["relaxed"] == []


def test_g2_without_a_baseline_keeps_the_erc_blocker(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _loaded(tmp_path)
    ctx.report["schematic"] = {"ok": True, "erc_violations": 2}
    _fake_schematic(monkeypatch, ctx)
    out = sf.g2_judge(ctx, _g2_verdict(2))
    assert out["blockers"] == ["ERC reports 2 violation(s)"] and fa.NO_ERC_BASELINE in out[
        "warnings"]
    assert _record(ctx)["erc"] == {"total": 2, "new": None, "baseline_total": None,
                                   "baseline": None}
    assert _record(ctx)["relaxed"] == []


def test_g2_an_unreadable_rebuilt_erc_report_is_a_warning_and_keeps_the_blocker(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shutil.copy(FAITH / "erc_baseline_k9.json", tmp_path / "source_erc.json")
    source = {**_equiv_design()["source"], "erc_baseline": "source_erc.json"}
    ctx = _loaded(tmp_path, _equiv_design(source=source))
    ctx.report["schematic"] = {"ok": True, "erc_violations": 5}     # no rb_erc.json
    _fake_schematic(monkeypatch, ctx)
    out = sf.g2_judge(ctx, _g2_verdict(5))
    assert "ERC reports 5 violation(s)" in out["blockers"]
    assert any(w.startswith("faithful: ERC was not judged against the source baseline")
               for w in out["warnings"])
    assert "error" in _record(ctx)["erc"]


def test_g2_erc_that_did_not_run_reads_no_report(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    source = {**_equiv_design()["source"], "erc_baseline": "missing_erc.json"}
    ctx = _loaded(tmp_path, _equiv_design(source=source))
    ctx.report["schematic"] = {"ok": True, "erc_violations": None}
    _fake_schematic(monkeypatch, ctx)
    out = sf.g2_judge(ctx, _g2_verdict(None))
    assert out["passed"] and "ERC did not run" in out["warnings"]
    assert _record(ctx)["erc"]["new"] is None and "error" not in _record(ctx)["erc"]


def test_g2_design_nets_that_differ_from_the_reference_block(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    design = _equiv_design()
    nets = {**design["nets"], "EN": [["R1", "2"], ["U1", "1"]],
            "GND": design["nets"]["GND"] + [["R2", "1"]]}
    ctx = _loaded(tmp_path, {**design, "nets": nets})
    _fake_schematic(monkeypatch, ctx)
    out = sf.g2_judge(ctx, _g2_verdict())
    assert not out["passed"]
    assert out["blockers"][0].startswith("G2 design: connectivity differs from the source")
    assert out["tasks"][-1]["auto"] is False
    g2 = _record(ctx)["equivalence"]["G2"]
    assert g2["design"]["equal"] is False and g2["schematic"]["equal"] is True


def test_g2_ignores_board_only_parts_and_dropped_refs(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    design = _equiv_design()
    nets = {**design["nets"], "A_B": [["U1", "4"]],
            "GND": [p for p in design["nets"]["GND"] if p[0] != "J1"]}
    comps = [c for c in design["components"] if c["ref"] != "J1"]
    dropped = {**design, "nets": nets, "components": comps,
               "variant": {"mode": "keep", "dropped_refs": ["J1"]}}
    ctx = _loaded(tmp_path, dropped)
    _fake_schematic(monkeypatch, ctx)
    out = sf.g2_judge(ctx, _g2_verdict())
    g2 = _record(ctx)["equivalence"]["G2"]
    assert out["passed"], out["blockers"]
    assert g2["design"]["ignored_refs"] == ["H1", "J1"] and g2["schematic"]["equal"] is True


def test_g2_exports_the_generated_schematic_in_place(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    ctx = _loaded(tmp_path)
    _fake_schematic(monkeypatch, ctx, calls=calls)
    sf.g2_judge(ctx, _g2_verdict())
    (sch, out, cli), = calls
    assert sch == Path(ctx.sch) and cli == "kicad-cli"
    assert out.parent != tmp_path and not out.exists()        # a temp dir, cleaned up


def test_g2_schematic_that_differs_blocks(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _loaded(tmp_path)
    xml = (EQUIV / "sch_k9.xml").read_text(encoding="utf-8")
    moved = tmp_path / "moved.xml"
    moved.write_text(xml.replace('<node ref="R2" pin="2"', '<node ref="R2" pin="9"'),
                     encoding="utf-8")
    assert moved.read_text(encoding="utf-8") != xml
    _fake_schematic(monkeypatch, ctx, moved)
    out = sf.g2_judge(ctx, _g2_verdict())
    assert out["blockers"] == [b for b in out["blockers"]
                               if b.startswith("G2 schematic: connectivity differs")]
    assert out["blockers"] and _record(ctx)["equivalence"]["G2"]["schematic"]["equal"] is False


def test_g2_without_kicad_cli_warns_and_does_not_claim_equality(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _loaded(tmp_path)
    monkeypatch.setattr(sf.tc, "kicad_cli", lambda: None)
    out = sf.g2_judge(ctx, _g2_verdict())
    sch = _record(ctx)["equivalence"]["G2"]["schematic"]
    assert out["passed"] and sch["equal"] is None and "kicad-cli" in sch["skipped"]
    assert any(w.startswith("G2 schematic: ") and "kicad-cli" in w for w in out["warnings"])


def test_g2_a_failed_export_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _loaded(tmp_path)
    _fake_schematic(monkeypatch, ctx)

    def fail(*_a: Any, **_k: Any) -> Path:
        raise RuntimeError("kicad-cli sch export netlist failed (exit 3)")

    monkeypatch.setattr(sf.ne, "export_schematic_xml", fail)
    out = sf.g2_judge(ctx, _g2_verdict())
    assert not out["passed"] and "exit 3" in out["blockers"][-1]
    assert out["blockers"][-1].startswith("G2 schematic: ")


def test_g2_a_missing_schematic_file_blocks(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _loaded(tmp_path)
    monkeypatch.setattr(sf.tc, "kicad_cli", lambda: "kicad-cli")
    out = sf.g2_judge(ctx, _g2_verdict())
    assert not out["passed"] and "does not exist" in out["blockers"][-1]


def test_g2_skips_the_schematic_when_generation_failed(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    ctx = _loaded(tmp_path)
    _fake_schematic(monkeypatch, ctx, calls=calls)
    ctx.report["schematic"] = {"ok": False}
    verdict = gates.g2_schematic({"ok": False}, REVIEW, None)
    out = sf.g2_judge(ctx, verdict)
    assert calls == [] and out["blockers"] == verdict["blockers"]
    assert _record(ctx)["equivalence"]["G2"]["schematic"]["equal"] is None


def test_g2_against_the_netlist_itself_when_there_is_no_reference(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = {"kind": "vision", "path": "scan.pdf"}
    ctx = _ctx(tmp_path, _equiv_design(source=source))
    sf.load(ctx)
    assert _record(ctx)["reference"] is None and sf.state(ctx).reference is None
    _fake_schematic(monkeypatch, ctx)
    out = sf.g2_judge(ctx, _g2_verdict())
    assert out["passed"] and _record(ctx)["equivalence"]["G2"]["schematic"]["equal"] is True


def test_g2_records_are_replaced_not_appended(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _loaded(tmp_path)
    _fake_schematic(monkeypatch, ctx)
    first = sf.g2_judge(ctx, _g2_verdict(0, ("VCC",)))
    rec = copy.deepcopy(_record(ctx))
    second = sf.g2_judge(ctx, _g2_verdict(0, ("VCC",)))
    assert first == second and _record(ctx) == rec and len(rec["relaxed"]) == 1


# ----------------------------------------------------------------------------
# G3
# ----------------------------------------------------------------------------
def _g3(ctx: SimpleNamespace, place: dict | None = None,
        courtyard: dict | None = None) -> tuple[dict, dict, dict]:
    cy, pl = sf.g3_inputs(ctx, courtyard or {"courtyard_violations": 0, "types": {}},
                          place or {"issues": [], "warnings": []})
    return cy, pl, gates.g3_placement(ctx.board_rep, cy, pl["issues"], pl["warnings"])


def test_g3_a_board_matching_the_reference_passes(tmp_path: Path) -> None:
    ctx = _loaded(tmp_path, drc=EMPTY_DRC)
    _, _, verdict = _g3(ctx)
    assert verdict["passed"], verdict["blockers"]
    g3 = _record(ctx)["equivalence"]["G3"]
    assert g3["equal"] is True and g3["stage"] == "G3"


def test_g3_one_pad_moved_to_another_net_fails_with_the_connectivity_blocker(
        tmp_path: Path) -> None:
    ctx = _loaded(tmp_path, drc=EMPTY_DRC)
    _move_r2_pad2_to_vcc(ctx.pcb)
    _, pl, verdict = _g3(ctx)
    assert not verdict["passed"]
    assert verdict["blockers"] == pl["issues"] and len(pl["issues"]) == 1
    assert pl["issues"][0].startswith("G3: connectivity differs from the source: NOT equivalent")
    assert "R2.2" in pl["issues"][0]
    assert verdict["tasks"][0]["auto"] is False                 # a designer task stops the ladder
    assert _record(ctx)["equivalence"]["G3"]["equal"] is False
    _freeze(tmp_path, _board_nets(Path(ctx.pcb)))               # the pad moved on the source too
    sf.load(ctx)
    assert _g3(ctx)[2]["passed"] and _record(ctx)["equivalence"]["G3"]["equal"] is True


def test_g3_board_only_parts_count_and_dropped_refs_do_not(tmp_path: Path) -> None:
    nets = {k: {p for p in v if p[0] != "H1"} for k, v in _board_nets().items()}
    _freeze(tmp_path, nets)
    ctx = _ctx(tmp_path, _equiv_design(), drc=EMPTY_DRC)
    sf.load(ctx)
    assert not _g3(ctx)[2]["passed"]                            # H1.1 is extra here
    variant = {"mode": "keep", "dropped_refs": ["H1"]}
    ctx = _ctx(tmp_path, _equiv_design(variant=variant), drc=EMPTY_DRC)
    sf.load(ctx)
    assert _g3(ctx)[2]["passed"]


def test_g3_a_kept_connector_becomes_a_warning(tmp_path: Path) -> None:
    ctx = _loaded(tmp_path, drc=EMPTY_DRC)
    place = {"issues": [CONNECTOR, "U1: something else"], "warnings": ["w0"]}
    _, pl, verdict = _g3(ctx, place)
    assert pl["issues"] == ["U1: something else"] and verdict["blockers"] == pl["issues"]
    assert pl["warnings"][0] == "w0" and CONNECTOR in pl["warnings"][1]
    assert place == {"issues": [CONNECTOR, "U1: something else"], "warnings": ["w0"]}
    assert _record(ctx)["relaxed"] == [f"G3: {pl['warnings'][1]}"]


def test_g3_structured_codes_win_over_the_text(tmp_path: Path) -> None:
    ctx = _loaded(tmp_path, drc=EMPTY_DRC)
    place = {"issues": ["J9: odd text", CONNECTOR], "warnings": [],
             "codes": ["connector_edge", "outline_open"]}
    _, pl, _ = _g3(ctx, place)
    assert pl["issues"] == [CONNECTOR] and "J9: odd text" in pl["warnings"][0]
    place = {**place, "codes": ["connector_edge"]}             # not parallel: texts decide
    _, pl, _ = _g3(ctx, place)
    assert pl["issues"] == ["J9: odd text"]


@pytest.mark.parametrize("rebuild, counted", [("keep", 0), ("reflow", 1)])
def test_g3_counts_only_our_courtyard_violations(tmp_path: Path, rebuild: str,
                                                 counted: int) -> None:
    board = FAITH / "mixed_board.sexpr"
    _freeze(tmp_path, _board_nets(board))
    ctx = _ctx(tmp_path, _equiv_design(rebuild=rebuild, components=[], nets={}), board=board,
               drc=FAITH / "drc_mixed.json")
    sf.load(ctx)
    cy, _, verdict = _g3(ctx, courtyard={"courtyard_violations": 1, "types": {"x": 1},
                                         "kicad_cli": "k"})
    assert cy["courtyard_violations"] == counted and cy["kicad_cli"] == "k"
    assert verdict["passed"] is (counted == 0)
    assert _record(ctx)["courtyard"] == {"reported": 1, "counted": counted}
    relaxed = [r for r in _record(ctx)["relaxed"] if "courtyard" in r]
    assert len(relaxed) == (1 if counted == 0 else 0)


def test_g3_without_a_drc_report_the_courtyard_count_stands(tmp_path: Path) -> None:
    ctx = _loaded(tmp_path)                                    # no rb_drc.json
    cy, pl, verdict = _g3(ctx, courtyard={"courtyard_violations": 2})
    assert cy == {"courtyard_violations": 2} and not verdict["passed"]
    assert any("no DRC report rb_drc.json" in w for w in pl["warnings"])
    Path(ctx.base + "_drc.json").write_text("not json", encoding="utf-8")
    _, pl, _ = _g3(ctx, courtyard={"courtyard_violations": 2})
    assert any("cannot be read" in w for w in pl["warnings"])


def test_g3_leaves_a_failed_board_build_alone(tmp_path: Path) -> None:
    ctx = _loaded(tmp_path, drc=EMPTY_DRC)
    ctx.board_rep = {"ok": False, "error": "boom"}
    cy, pl = {"courtyard_violations": 0}, {"issues": [CONNECTOR], "warnings": []}
    assert sf.g3_inputs(ctx, cy, pl) == (cy, pl)
    assert _record(ctx)["equivalence"]["G3"]["equal"] is None


def test_g3_an_unreadable_board_blocks(tmp_path: Path) -> None:
    ctx = _loaded(tmp_path, drc=EMPTY_DRC)
    Path(ctx.pcb).write_text("not a board", encoding="utf-8")
    _, pl, verdict = _g3(ctx)
    assert not verdict["passed"] and pl["issues"][0].startswith(
        "G3: connectivity could not be compared with the source")


def test_g3_applied_twice_gives_the_same_inputs(tmp_path: Path) -> None:
    ctx = _loaded(tmp_path, drc=EMPTY_DRC)
    _move_r2_pad2_to_vcc(ctx.pcb)
    cy, pl = sf.g3_inputs(ctx, {"courtyard_violations": 0, "types": {}},
                          {"issues": [CONNECTOR], "warnings": []})
    assert sf.g3_inputs(ctx, cy, pl) == (cy, pl)
    assert len(pl["issues"]) == 1 and CONNECTOR in pl["warnings"][0]
    late = {**pl, "issues": pl["issues"] + ["late issue"]}    # added after us: kept, once
    _, again = sf.g3_inputs(ctx, cy, late)
    assert sorted(again["issues"]) == sorted(late["issues"]) and again["warnings"] == pl[
        "warnings"]
    edited = {**pl, "issues": []}                              # ours removed: a new base
    _, again = sf.g3_inputs(ctx, cy, edited)
    assert again["issues"] == pl["issues"] and again["warnings"] == pl["warnings"]


# ----------------------------------------------------------------------------
# G4
# ----------------------------------------------------------------------------
def _drc_rep(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    types: dict[str, int] = {}
    for v in data["violations"]:
        types[v["type"]] = types.get(v["type"], 0) + 1
    return {"errors": len(data["violations"]), "unconnected": len(data["unconnected_items"]),
            "types": types, "kicad_cli": "kicad-cli"}


def _mixed_ctx(tmp: Path, rebuild: str) -> SimpleNamespace:
    board = FAITH / "mixed_board.sexpr"
    _freeze(tmp, _board_nets(board))
    ctx = _ctx(tmp, _equiv_design(rebuild=rebuild, components=[], nets={}), board=board,
               drc=FAITH / "drc_mixed.json")
    sf.load(ctx)
    return ctx


def test_g4_keep_mode_blocks_only_on_our_copper(tmp_path: Path) -> None:
    ctx = _mixed_ctx(tmp_path, "keep")
    drc = _drc_rep(FAITH / "drc_mixed.json")
    split = fa.split_drc(FAITH / "drc_mixed.json", sf.state(ctx).profile)
    out_drc, routed = sf.g4_inputs(ctx, drc, {"issues": [], "warnings": ["w0"]})
    assert out_drc == {**drc, "errors": 1, "types": {"clearance": 1}}
    assert out_drc["unconnected"] == drc["unconnected"] == 1 and drc["errors"] > 1
    verdict = gates.g4_layout({"unconnected": 0}, out_drc, routed["issues"], routed["warnings"])
    assert verdict["blockers"] == ["DRC: 1 violation(s) (clearancex1)",
                                   "DRC: 1 unconnected item(s)"]
    assert routed["warnings"][0] == "w0"
    assert routed["warnings"][1:] == [f"faithful: {t}" for t in split.placement + split.intrinsic]
    rec = _record(ctx)
    assert rec["drc"] == {**split.as_dict(), "report": ctx.base + "_drc.json"}
    assert rec["equivalence"]["G4"]["equal"] is True
    assert [r for r in rec["relaxed"] if r.startswith("G4: ")] == [
        f"G4: {len(split.placement)} DRC violation(s) from the source placement and "
        f"{len(split.intrinsic)} inside one footprint are warnings, not blockers"]


def test_g4_reflow_mode_keeps_the_courtyard_overlap_ours(tmp_path: Path) -> None:
    ctx = _mixed_ctx(tmp_path, "reflow")
    out_drc, routed = sf.g4_inputs(ctx, _drc_rep(FAITH / "drc_mixed.json"),
                                   {"issues": [], "warnings": []})
    assert out_drc["errors"] == 2 and out_drc["types"] == {"clearance": 1,
                                                          "courtyards_overlap": 1}
    assert all("inside JP1" in w for w in routed["warnings"])


def test_g4_a_moved_pad_on_the_routed_board_blocks(tmp_path: Path) -> None:
    ctx = _loaded(tmp_path, drc=EMPTY_DRC)
    _move_r2_pad2_to_vcc(ctx.pcb)
    _, routed = sf.g4_inputs(ctx, {"errors": 0, "unconnected": 0, "types": {}},
                             {"issues": [], "warnings": []})
    verdict = gates.g4_layout({"unconnected": 0}, {"errors": 0}, routed["issues"],
                              routed["warnings"])
    assert not verdict["passed"] and verdict["blockers"][0].startswith(
        "G4: connectivity differs from the source")
    assert _record(ctx)["equivalence"]["G4"]["equal"] is False


def test_g4_the_emc_rejudge_does_not_duplicate_findings(tmp_path: Path) -> None:
    ctx = _mixed_ctx(tmp_path, "keep")
    drc = _drc_rep(FAITH / "drc_mixed.json")
    first_drc, routed = sf.g4_inputs(ctx, drc, {"issues": ["width"], "warnings": []})
    again_drc, again = sf.g4_inputs(ctx, dict(drc), routed)       # fresh DRC, same routed
    assert (again_drc, again) == (first_drc, routed)
    assert again["issues"] == ["width"]


def test_g4_without_a_drc_report_judges_every_violation_as_ours(tmp_path: Path) -> None:
    ctx = _loaded(tmp_path)                                    # no rb_drc.json
    drc = {"errors": 3, "unconnected": 0, "types": {"clearance": 3}}
    out_drc, routed = sf.g4_inputs(ctx, drc, {"issues": [], "warnings": []})
    assert out_drc is drc
    assert any("every DRC violation is judged as ours" in w for w in routed["warnings"])
    assert _record(ctx)["drc"]["split"] is False


# ----------------------------------------------------------------------------
# the record the review renders (import_review, F46-08)
# ----------------------------------------------------------------------------
def test_the_record_renders_in_the_import_review(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _loaded(tmp_path, drc=EMPTY_DRC)
    _fake_schematic(monkeypatch, ctx)
    sf.g2_judge(ctx, _g2_verdict(0, ("VCC",)))
    _g3(ctx)
    _move_r2_pad2_to_vcc(ctx.pcb)
    sf.g4_inputs(ctx, {"errors": 0, "unconnected": 0, "types": {}},
                 {"issues": [], "warnings": []})
    json.dumps(ctx.report)                                     # JSON-ready
    lines = ir.render_import_section(ctx.report, ctx.design, None)
    rows = [ln for ln in lines if ln.startswith("| G")]
    assert [r.split(" | ")[0] for r in rows] == ["| G2 design", "| G2 schematic", "| G3", "| G4"]
    assert "| G4 | no |" in rows[-1]
    assert any(ln.startswith("- DRC split: 0 ours") for ln in lines)
    items = ir.open_items(ctx.report, ctx.design, None)
    assert items == ["G4: connectivity differs from the source: "
                     + _record(ctx)["equivalence"]["G4"]["summary"]]


# ----------------------------------------------------------------------------
# live: kicad-cli exports the generated lm358_buffer schematic (G2 path)
# ----------------------------------------------------------------------------
@pytest.fixture(scope="module")
def live_cli() -> str:
    cli = sf.tc.kicad_cli()
    if not cli or not sf.tc.symbols_dir():
        pytest.skip("no kicad-cli / KiCad symbol library installed")
    return cli


def _lm358_ctx(tmp: Path, reference: str) -> SimpleNamespace:
    design = json.loads(LM358.read_text(encoding="utf-8"))
    source = {"kind": "kicad", "path": "src/lm358.kicad_pro", "reference": reference,
              "erc_baseline": "source_erc.json"}
    return _ctx(tmp, {**design, "source": source, "rebuild": "reflow"}, board=None)


def test_live_g2_exports_the_generated_lm358_schematic(tmp_path: Path, live_cli: str) -> None:
    import kicad_server as k

    nets = ne.netlist_pad_nets(json.loads(LM358.read_text(encoding="utf-8")))
    _freeze(tmp_path, nets)
    moved = {**nets, "IN_A": nets["IN_A"] - {("J1", "3")},
             "IN_B": nets["IN_B"] | {("J1", "3")}}
    _freeze(tmp_path, moved, "moved_padnets.json")
    ctx = _lm358_ctx(tmp_path, "reference_padnets.json")
    built = k.build_schematic_from_netlist(ctx.netlist, ctx.sch, backend="file")
    assert built["ok"] and built.get("erc_violations") is not None, built
    shutil.copy(ctx.base + "_erc.json", tmp_path / "source_erc.json")
    verdict = gates.g2_schematic(built, REVIEW, None)

    ctx.report["schematic"] = {kk: built.get(kk) for kk in ("ok", "erc_violations")}
    sf.load(ctx)
    out = sf.g2_judge(ctx, verdict)
    rec = _record(ctx)
    assert out["passed"], out["blockers"]
    assert rec["equivalence"]["G2"]["schematic"]["equal"] is True
    assert rec["erc"]["new"] == [] and rec["erc"]["total"] == built["erc_violations"]

    moved_ctx = _lm358_ctx(tmp_path, "moved_padnets.json")
    moved_ctx.report["schematic"] = ctx.report["schematic"]
    sf.load(moved_ctx)
    out = sf.g2_judge(moved_ctx, verdict)
    assert [b.split(":")[0] for b in out["blockers"]] == ["G2 design", "G2 schematic"]
    assert _record(moved_ctx)["equivalence"]["G2"]["schematic"]["equal"] is False
