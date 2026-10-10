"""The harness review on every exit (ROADMAP D5): run_pipeline.run writes
<stem>_pipeline_report.json AND <stem>_review.md whatever happens, records its
runtime decisions in the append-only <stem>.decisions.jsonl (G2/G3/G4
auto-remediations, the routing method, accepted BOM sourcing, safety flags),
adds the report keys review_report renders (board_file, pre_k11_snapshot,
routing), carries the D5 safety warning at G5, and main() writes a failing
report + review for a usage error once the netlist path is known.

Every KiCad-facing call is a scripted stand-in (tests/fixtures/pipeline_world.py).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import decisions as dec
import libtable
import remediate
import review_report
import review_sources
import rp_context
import run_pipeline as rp
import toolchain as tc
from fixtures import pipeline_world
from fixtures.pipeline_world import CLEAN_DRC, DESIGN, K9_HEADER, K11_HEADER, OK_ANALYSIS, World

world = pipeline_world.world        # the fixture (an assignment: see pipeline_world)

CHARGER_DESIGN = {
    "title": "single-cell charger",
    "components": [
        {"ref": "U1", "lib_id": "Battery_Management:TP4056", "value": "TP4056",
         "footprint": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"},
        {"ref": "R1", "lib_id": "Device:R", "value": "1k2",
         "footprint": "Resistor_SMD:R_0603_1608Metric"}],
    "nets": {"VBAT": [["U1", "5"], ["R1", "1"]], "GND": [["U1", "3"], ["R1", "2"]]},
}


def _review(world: World) -> str:
    return (world.dir / "netlist_review.md").read_text(encoding="utf-8")


def _persisted(world: World) -> dict:
    return json.loads((world.dir / "netlist_pipeline_report.json").read_text(encoding="utf-8"))


def _headings(markdown: str) -> list[str]:
    return [line[3:] for line in markdown.splitlines() if line.startswith("## ")]


def _gate(rep: dict, name: str) -> dict:
    return next(g for g in rep["gates"] if g["gate"] == name)


def _log(world: World) -> list[dict]:
    return dec.read_decisions(world.netlist)          # raises on any malformed line


def _verdict_line(markdown: str) -> str:
    return next(line for line in markdown.splitlines() if line.startswith("- **Verdict"))


def _raising(exc: Exception):
    def fail(*args, **kw):
        raise exc
    return fail


# --- a passing run ---------------------------------------------------------------

def test_a_pass_run_writes_decisions_and_a_review_with_every_section_in_order(world):
    rep = world.run()
    review = world.dir / "netlist_review.md"
    assert rep["PASS"] is True
    assert rep["review"] == str(review)
    assert _headings(review.read_text(encoding="utf-8")) == list(review_report.SECTIONS)
    assert _verdict_line(_review(world)).startswith("- **Verdict: PASS**: all 5 gates passed")
    assert rep["decisions_log"] == str(world.dir / "netlist.decisions.jsonl")
    stages = [e["stage"] for e in _log(world)]
    assert stages == ["routing", "G5"]                   # routing method + BOM sourcing
    assert "decision_log_errors" not in rep and "review_error" not in rep
    assert _persisted(world) == json.loads(json.dumps(rep))


def test_the_report_carries_the_keys_the_review_renders(world):
    rep = world.run(route="auto")
    assert rep["board_file"] == {"path": str(world.pcb), "format_version": 20241229,
                                 "generator": "pcbnew", "generator_version": "9.0"}
    assert rep["routing"] == {"method": "freerouting", "requested": "auto",
                              "why": rp_context.ROUTING_WHY}
    assert rp_context.ROUTING_WHY == ("harness default: Freerouting on a canonical DSN "
                                      "(deterministic, no tokens)")
    assert "pre_k11_snapshot" not in rep
    text = _review(world)
    assert "- Method: freerouting (requested: auto)" in text
    assert f"- Why: {rp_context.ROUTING_WHY}" in text
    assert "pre-KiCad 11 dialect" in text
    assert "Pre-KiCad 11 snapshot: none needed" in text


def test_an_existing_pre_k11_snapshot_is_reported(world):
    snapshot = Path(tc.pre_k11_snapshot_path(str(world.pcb)))
    snapshot.write_text(K9_HEADER, encoding="utf-8")
    rep = world.run()
    assert rep["pre_k11_snapshot"] == str(snapshot)
    assert f"Pre-KiCad 11 snapshot: `{snapshot}`" in _review(world)


def test_the_routing_decision_is_recorded_once_per_run_and_names_the_cache(world):
    first = world.run(route="auto")
    second = world.run(route="auto")                 # nothing moved: a route cache hit
    assert first["routing"]["method"] == "freerouting"
    assert second["route_cache"] == "hit" and second["routing"]["method"] == "route cache"
    routing = [e for e in _log(world) if e["stage"] == "routing"]
    assert len(routing) == 2
    assert "Freerouting" in routing[0]["decision"]
    assert "route cache" in routing[1]["decision"]
    assert routing[0]["rationale"] == rp_context.ROUTING_WHY
    assert routing[0]["alternatives"] and routing[0]["confidence"] == "high"


def test_a_routing_record_set_by_a_stage_is_kept(world, monkeypatch):
    """A later stage (route modes) may record routing itself: halt keeps it and
    records no second routing decision."""
    own = {"method": "agent", "requested": "agent", "why": "the user asked for it"}
    real = rp.STAGES[-1]

    def g5_and_routing(ctx: rp_context.RunContext) -> bool:
        ctx.report["routing"] = own
        return real(ctx)

    monkeypatch.setattr(rp, "STAGES", (*rp.STAGES[:-1], g5_and_routing))
    rep = world.run()
    assert rep["routing"] == own
    assert [e["stage"] for e in _log(world)] == ["G5"]


def test_bom_sourcing_accepted_as_warnings_is_a_medium_confidence_g5_decision(world):
    world.run()
    bom = next(e for e in _log(world) if e["stage"] == "G5")
    assert bom["confidence"] == "med"
    assert "R1, R2" in bom["decision"]
    assert bom["override"] == "require_sourcing"
    assert bom["alternatives"]


@pytest.mark.parametrize("change", [
    lambda d: d.__setitem__("require_sourcing", True),
    lambda d: [c.__setitem__("lcsc", "C25804") for c in d["components"]],
], ids=["required", "sourced"])
def test_no_bom_decision_when_sourcing_is_required_or_complete(world, change):
    design = copy.deepcopy(DESIGN)
    change(design)
    world.write_design(design)
    world.run()
    assert [e for e in _log(world) if e["stage"] == "G5"] == []


# --- remediations become decisions ------------------------------------------------

def test_a_g2_auto_remediation_becomes_a_decision(world, monkeypatch):
    missing = {"decoupling": {"uncovered": [{"ref": "R1", "rails_without_cap": ["+5V"]}]},
               "flags": {}, "errors": []}
    world.analyses = [missing, OK_ANALYSIS]

    def fake_apply(design: dict, tasks: list) -> tuple[dict, list]:
        fixed = copy.deepcopy(design)
        fixed["components"].append({"ref": "C9", "footprint": "Capacitor_SMD:C_0402"})
        return fixed, ["added C9 for +5V"]

    monkeypatch.setattr(remediate, "apply", fake_apply)
    rep = world.run()
    assert rep["remediation"] == [{"gate": "G2", "round": 1, "applied": ["added C9 for +5V"]}]
    g2 = [e for e in _log(world) if e["stage"] == "G2"]
    assert len(g2) == 1
    assert g2[0]["question"] == ("G2 failed in round 1: apply the automatic corrective "
                                 "action?")
    assert g2[0]["decision"] == "added C9 for +5V"
    assert g2[0]["rationale"] == "standard remedy for an auto task (gates.py)"
    assert g2[0]["confidence"] == "high"
    assert g2[0]["source"] == "run_pipeline G2 auto-remediation"
    assert "### G2" in _review(world)


def test_log_remediation_records_every_item_except_open_ones(tmp_path):
    ctx = rp_context.RunContext(netlist=str(tmp_path / "n.json"), w=1, h=1)
    ctx.log_remediation("G4", 2, ["bridged gap on N1", "OPEN: clearance needs a re-route"])
    assert ctx.report["remediation"] == [
        {"gate": "G4", "round": 2,
         "applied": ["bridged gap on N1", "OPEN: clearance needs a re-route"]}]
    entries = dec.read_decisions(tmp_path / "n.json")
    assert [(e["stage"], e["decision"]) for e in entries] == [("G4", "bridged gap on N1")]
    assert entries[0]["source"] == "run_pipeline G4 auto-remediation"


def test_log_remediation_without_record_writes_no_decision(tmp_path):
    ctx = rp_context.RunContext(netlist=str(tmp_path / "n.json"), w=1, h=1)
    ctx.log_remediation("G3", 1, ["re-placed at pack_scale 1.5"], record=False)
    assert ctx.remediation == [{"gate": "G3", "round": 1,
                                "applied": ["re-placed at pack_scale 1.5"]}]
    assert not dec.decisions_path(tmp_path / "n.json").exists()


def test_g3_and_g4_remediations_of_a_run_are_logged(world):
    world.drcs = [{**CLEAN_DRC, "types": {"courtyards_overlap": 2}}, CLEAN_DRC]
    world.run()
    g3 = [e for e in _log(world) if e["stage"] == "G3"]
    assert [e["decision"] for e in g3] == ["re-placed at pack_scale 1.5"]


# --- ctx.decide --------------------------------------------------------------------

def test_decide_appends_one_entry_and_returns_it(tmp_path):
    ctx = rp_context.RunContext(netlist=str(tmp_path / "n.json"), w=1, h=1)
    entry = ctx.decide("G1", "Which mode?", "napkin", "harness default",
                       confidence="med", alternatives=("strict",), override="mode")
    assert entry is not None and entry["id"].startswith("dec-")
    assert entry["source"] == "run_pipeline" and entry["reversible"] is True
    assert dec.read_decisions(tmp_path / "n.json") == [entry]


def test_decide_never_raises_an_invalid_decision(tmp_path):
    ctx = rp_context.RunContext(netlist=str(tmp_path / "n.json"), w=1, h=1)
    assert ctx.decide("G9", "q", "d", "r") is None
    assert ctx.decide("G1", "q", " ", "r") is None
    assert ctx.decide("G1", "q", "d", "r", alternatives="strict") is None   # not a list
    errors = ctx.report["decision_log_errors"]
    assert len(errors) == 3 and "stage must be one of" in errors[0]
    assert "decision must not be blank" in errors[1]
    assert "alternatives must be a list of strings" in errors[2]
    assert not dec.decisions_path(tmp_path / "n.json").exists()


def test_decide_never_raises_a_write_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(dec, "append_decision", _raising(PermissionError("log is read-only")))
    ctx = rp_context.RunContext(netlist=str(tmp_path / "n.json"), w=1, h=1)
    assert ctx.decide("G2", "q", "d", "r") is None
    assert ctx.report["decision_log_errors"] == ["PermissionError: log is read-only"]


def test_an_unwritable_decision_log_sets_decision_log_errors(world):
    dec.decisions_path(world.netlist).mkdir()             # a directory where the log goes
    rep = world.run()
    assert rep["PASS"] is True
    assert len(rep["decision_log_errors"]) == 2           # routing + BOM sourcing
    assert "decisions_log" not in rep
    text = _review(world)
    assert _headings(text) == list(review_report.SECTIONS)
    assert "Fix the decision log:" in text and "cannot read the decision log" in text
    assert _persisted(world)["decision_log_errors"] == rep["decision_log_errors"]


# --- failures still write the review -------------------------------------------------

def test_a_gate_failure_writes_the_review(world):
    world.pcb.write_text(K9_HEADER, encoding="utf-8")    # a stale board of an earlier run
    world.lint = [{"ok": False, "errors": ["E3 single-pin net N9"], "warnings": []}]
    rep = world.run()
    assert rep["PASS"] is False and world.names() == ["lint"]
    text = _review(world)
    assert _headings(text) == list(review_report.SECTIONS)
    assert _verdict_line(text).startswith("- **Verdict: FAIL**: stopped at G1")
    assert "E3 single-pin net N9" in text
    assert "routing" not in rep and "board_file" not in rep     # never reached
    assert rep["review"] == str(world.dir / "netlist_review.md")


def test_a_crash_writes_the_review_and_still_raises(world):
    world.routes = [RuntimeError("kicad-cli not found")]
    with pytest.raises(RuntimeError, match="kicad-cli not found"):
        world.run()
    saved = _persisted(world)
    assert saved["error"] == "RuntimeError: kicad-cli not found"
    text = _review(world)
    assert _verdict_line(text) == ("- **Verdict: FAIL**: the run crashed: RuntimeError: "
                                   "kicad-cli not found")
    assert saved["review"] == str(world.dir / "netlist_review.md")
    assert saved["board_file"]["format_version"] == 20241229   # the board was built


def test_an_unreadable_netlist_writes_a_review_without_a_design(world):
    world.netlist.write_text("{ not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        world.run()
    assert _persisted(world)["review"] == str(world.dir / "netlist_review.md")
    assert "No components" in _review(world)


def test_a_review_failure_keeps_the_json_and_sets_review_error(world, monkeypatch):
    monkeypatch.setattr(review_report, "render_review", _raising(ValueError("cannot render")))
    rep = world.run()
    assert rep["PASS"] is True
    assert rep["review_error"] == "ValueError: cannot render"
    assert "review" not in rep and not (world.dir / "netlist_review.md").exists()
    assert _persisted(world) == json.loads(json.dumps(rep))


def test_a_review_failure_never_masks_the_runs_own_error(world, monkeypatch):
    monkeypatch.setattr(review_report, "write_review", _raising(OSError("disk full")))
    world.routes = [RuntimeError("kicad-cli not found")]
    with pytest.raises(RuntimeError, match="kicad-cli not found"):
        world.run()
    saved = _persisted(world)
    assert saved["error"] == "RuntimeError: kicad-cli not found"
    assert saved["review_error"] == "OSError: disk full"


def test_a_report_write_failure_is_listed_in_the_review(world, monkeypatch, capsys):
    monkeypatch.setattr(rp_context, "REPORT_SUFFIX", "/no/such/dir/r.json")
    rep = world.run()
    assert rep["PASS"] is True and "r.json" in rep["pipeline_report_error"]
    assert "could not write" in capsys.readouterr().err
    assert "Pipeline report not written" in _review(world)


# --- library sources -------------------------------------------------------------------

def _stock_libraries(root: Path) -> tuple[str, str]:
    syms, fps = root / "symbols", root / "footprints"
    syms.mkdir()
    (syms / "Device.kicad_sym").write_text("(kicad_symbol_lib (version 20241209))\n",
                                           encoding="utf-8")
    pretty = fps / "Resistor_SMD.pretty"
    pretty.mkdir(parents=True)
    (pretty / "R_0603_1608Metric.kicad_mod").write_text(
        '(footprint "R_0603_1608Metric" (version 20241229))\n', encoding="utf-8")
    return str(syms), str(fps)


def test_the_parts_table_names_the_stock_library_of_every_part(world, monkeypatch):
    syms, fps = _stock_libraries(world.dir)
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": syms)
    monkeypatch.setattr(tc, "footprints_dir", lambda explicit="": fps)
    world.run()
    text = _review(world)
    rows = [line for line in text.splitlines() if line.startswith(("| R1 |", "| R2 |"))]
    assert len(rows) == 2 and all(row.endswith("| stock |") for row in rows)
    assert "Library sources: stock 2." in text


def test_a_library_table_error_is_reported_and_the_review_still_written(world, monkeypatch):
    monkeypatch.setattr(review_sources, "library_sources",
                        _raising(libtable.LibTableError("fp-lib-table: unbalanced parentheses")))
    rep = world.run()
    assert rep["library_sources_error"] == "fp-lib-table: unbalanced parentheses"
    assert "review_error" not in rep
    assert "Library sources were not supplied" in _review(world)


# --- G5 safety warning ------------------------------------------------------------------

def test_g5_carries_the_safety_warning_for_a_tp4056_charger(world):
    world.write_design(copy.deepcopy(CHARGER_DESIGN))
    rep = world.run()
    warnings = _gate(rep, "G5")["warnings"]
    safety = [w for w in warnings if w.startswith("HUMAN REVIEW REQUIRED: Li-ion charging:")]
    assert len(safety) == 1 and "U1 (TP4056)" in safety[0]
    assert _review(world).startswith("> **HUMAN REVIEW REQUIRED**")
    flagged = [e for e in _log(world) if e["stage"] == "G5" and e["confidence"] == "high"]
    assert len(flagged) == 1 and "HUMAN REVIEW REQUIRED" in flagged[0]["decision"]


def test_an_ordinary_design_gets_no_safety_warning(world):
    rep = world.run()
    assert not any("HUMAN REVIEW" in w for w in _gate(rep, "G5")["warnings"])
    assert all(e["confidence"] != "high" or e["stage"] != "G5" for e in _log(world))


def test_no_safety_decision_when_the_run_stops_before_g5(world):
    world.write_design(copy.deepcopy(CHARGER_DESIGN))
    world.lint = [{"ok": False, "errors": ["E3"], "warnings": []}]
    world.run()
    assert _log(world) == []
    assert _review(world).startswith("> **HUMAN REVIEW REQUIRED**")   # the review still flags it


# --- the cache manifest's OSError branch -------------------------------------------------

def test_an_unwritable_cache_manifest_is_reported_and_the_run_reaches_g5(world):
    (world.dir / "netlist.cache.json").mkdir()            # write_text fails on a directory
    rep = world.run()
    assert "could not write" in rep["route_cache_error"]
    assert [g["gate"] for g in rep["gates"]] == ["G1", "G2", "G3", "G4", "G5"]
    assert rep["PASS"] is True
    assert "Route cache manifest not written" in _review(world)


# --- main(): usage errors ---------------------------------------------------------------

FIXED = {"title": "all fixed", "components": [
    {"ref": "R1", "lib_id": "Device:R", "value": "1k", "footprint": "R:R", "x": 1, "y": 2},
    {"ref": "R2", "lib_id": "Device:R", "value": "1k", "footprint": "R:R", "at": [5, 5, 0]}],
    "nets": {"N1": [["R1", "1"], ["R2", "1"]]}}


def test_a_usage_error_with_a_readable_netlist_writes_the_report(world, capsys):
    world.write_design(copy.deepcopy(FIXED))
    assert rp.main([str(world.netlist)]) == 2
    assert capsys.readouterr().err.startswith(
        "run_pipeline: error: every component has a fixed position")
    saved = _persisted(world)
    assert saved["PASS"] is False and saved["gates"] == []
    assert saved["error"].startswith("usage: every component has a fixed position")
    text = _review(world)
    assert _headings(text) == list(review_report.SECTIONS)
    assert "# Design review: all fixed" in text and "| R1 |" in text
    assert world.calls == []                               # nothing ran


def test_a_usage_error_with_an_unreadable_netlist_writes_a_report_without_a_design(world):
    world.netlist.write_text("{ not json", encoding="utf-8")
    assert rp.main([str(world.netlist)]) == 2
    saved = _persisted(world)
    assert saved["error"].startswith("usage: cannot read the netlist")
    assert "No components" in _review(world)


def test_a_missing_netlist_in_an_existing_folder_writes_the_report(tmp_path, world):
    missing = tmp_path / "missing.json"
    assert rp.main([str(missing)]) == 2
    saved = json.loads((tmp_path / "missing_pipeline_report.json").read_text(encoding="utf-8"))
    assert saved["PASS"] is False and saved["error"].startswith("usage: cannot read")
    assert (tmp_path / "missing_review.md").is_file()


def test_a_netlist_in_a_missing_folder_writes_nothing(tmp_path, world):
    assert rp.main([str(tmp_path / "nowhere" / "n.json")]) == 2
    assert not (tmp_path / "nowhere").exists()


def test_a_malformed_command_line_writes_nothing(tmp_path, world, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    assert rp.main(["n.json", "30"]) == 2                  # the path is never known
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_a_kicad_11_board_is_reported_with_its_format(world):
    world.header = K11_HEADER
    rep = world.run()
    assert rep["board_file"]["generator_version"] == "10.99"
    assert "KiCad 11 dialect" in _review(world)
