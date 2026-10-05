"""review_tools: the napkin-mode MCP surface (ROADMAP D5): record_decision
appends to <stem>.decisions.jsonl, review_report renders <stem>_review.md
from the netlist, the decision log, library sources and (read-only) the
harness's <stem>_pipeline_report.json. Pure unit tier: the stock library
dirs are tmp dirs, no KiCad is discovered or run.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

import decisions as dec
import design_report
import review_tools as rt
import toolchain as tc

EDITOR_SYM = '(kicad_symbol_lib (version 20241209) (generator "kicad_symbol_editor"))\n'
EDITOR_FP = '(footprint "R_0603_1608Metric" (version 20241229) (generator "pcbnew"))\n'
GATE_NAMES = {"G1": "Requirements / netlist review", "G2": "Schematic review",
              "G3": "Placement review", "G4": "Layout / board review",
              "G5": "Fabrication package"}


def _decision(**over) -> dict:
    base = {"stage": "parts", "question": "Which 10k resistor?",
            "decision": "Yageo RC0603FR-0710KL", "rationale": "stock JLCPCB basic part",
            "confidence": "med", "source": "policy:basic-parts"}
    return {**base, **over}


def _passing_report(netlist: str | None) -> dict:
    gates = [{"gate": g, "name": n, "feeds": "x", "passed": True, "blockers": [],
              "warnings": [], "tasks": []} for g, n in GATE_NAMES.items()]
    report = {"gates": gates, "PASS": True}
    return report if netlist is None else {"netlist": netlist, **report}


def _section(md: str, title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", md, re.S | re.M)
    assert m, f"section '{title}' missing"
    return m.group(1)


@pytest.fixture(autouse=True)
def stock(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """Tmp stock libraries stand in for the KiCad install (never discovered)."""
    syms, fps = tmp_path / "stock" / "symbols", tmp_path / "stock" / "footprints"
    syms.mkdir(parents=True)
    (syms / "Device.kicad_sym").write_text(EDITOR_SYM, encoding="utf-8")
    pretty = fps / "Resistor_SMD.pretty"
    pretty.mkdir(parents=True)
    (pretty / "R_0603_1608Metric.kicad_mod").write_text(EDITOR_FP, encoding="utf-8")
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": str(syms))
    monkeypatch.setattr(tc, "footprints_dir", lambda explicit="": str(fps))
    return syms, fps


@pytest.fixture
def netlist(tmp_path: Path) -> Path:
    d = tmp_path / "proj"
    d.mkdir()
    path = d / "divider.json"
    design = {"name": "divider", "components": [
        {"ref": "R1", "lib_id": "Device:R", "value": "10k",
         "footprint": "Resistor_SMD:R_0603_1608Metric"},
        {"ref": "R2", "lib_id": "Device:R", "value": "10k",
         "footprint": "Resistor_SMD:R_0603_1608Metric"}],
        "nets": {"IN": [["R1", "1"]], "MID": [["R1", "2"], ["R2", "1"]], "GND": [["R2", "2"]]}}
    path.write_text(json.dumps(design, indent=2), encoding="utf-8")
    return path


# --- registration -------------------------------------------------------------
def test_tools_tuple_holds_both_tools():
    assert rt.TOOLS == (rt.record_decision, rt.review_report)
    for fn in rt.TOOLS:
        assert fn.__doc__ and len(fn.__doc__) > 200, fn.__name__


def test_module_doc_no_longer_promises_to_write_the_pipeline_report():
    doc = rt.__doc__
    assert "TOOLS" in doc and "ROADMAP_DECISIONS" in doc
    assert "never writes" in doc and "_pipeline_report.json" in doc


# --- record_decision ------------------------------------------------------------
def test_record_decision_appends_one_entry(netlist):
    out = rt.record_decision(str(netlist), **_decision(alternatives=["Vishay CRCW0603"],
                                                       reversible=False,
                                                       override="components[R1].mpn"))
    assert out["ok"] is True
    assert out["log"] == str(netlist.parent / "divider.decisions.jsonl")
    entry = out["entry"]
    assert entry["alternatives"] == ["Vishay CRCW0603"] and entry["reversible"] is False
    assert entry["override"] == "components[R1].mpn" and entry["stage"] == "parts"
    assert dec.read_decisions(netlist) == [entry]


def test_record_decision_alternatives_none_is_stored_as_empty(netlist):
    out = rt.record_decision(str(netlist), **_decision(), alternatives=None)
    assert out["ok"] is True and out["entry"]["alternatives"] == []
    assert dec.read_decisions(netlist)[0]["alternatives"] == []


def test_record_decision_rejects_an_invalid_stage(netlist):
    out = rt.record_decision(str(netlist), **_decision(stage="G6"))
    assert out["ok"] is False and "stage" in out["reason"]
    assert not dec.decisions_path(netlist).exists()


def test_record_decision_needs_the_netlist_directory(tmp_path):
    missing = tmp_path / "nope" / "n.json"
    out = rt.record_decision(str(missing), **_decision())
    assert out["ok"] is False
    assert str(missing.parent) in out["reason"] and "does not exist" in out["reason"]
    assert not missing.parent.exists()


@pytest.mark.parametrize("bad", ["", "   "])
def test_record_decision_needs_a_netlist_path(bad):
    out = rt.record_decision(bad, **_decision())
    assert out["ok"] is False and "netlist" in out["reason"]


def test_record_decision_refuses_a_directory_as_the_netlist(tmp_path):
    out = rt.record_decision(str(tmp_path), **_decision())
    assert out["ok"] is False and "directory" in out["reason"]


def test_record_decision_reports_an_unwritable_log(netlist):
    dec.decisions_path(netlist).mkdir()                       # the log path is a folder
    out = rt.record_decision(str(netlist), **_decision())
    assert out["ok"] is False and out["reason"]
    assert not dec.lock_path(netlist).exists()                # the lock was released


def test_record_decision_reports_a_held_lock(netlist, monkeypatch):
    monkeypatch.setattr(dec, "LOCK_TIMEOUT_S", 0.05)
    dec.lock_path(netlist).write_text("1\n", encoding="ascii")
    out = rt.record_decision(str(netlist), **_decision())
    assert out["ok"] is False and "lock" in out["reason"]
    assert not dec.decisions_path(netlist).exists()


# --- review_report ------------------------------------------------------------------
def test_review_without_a_pipeline_report(netlist):
    out = rt.review_report(str(netlist))
    assert out["ok"] is True, out
    review = netlist.parent / "divider_review.md"
    assert out["review"] == str(review) and review.is_file()
    assert out["pipeline_report"] is None
    assert not (netlist.parent / "divider_pipeline_report.json").exists()   # never written
    assert "no gate ran" in out["verdict"] and out["verdict"].startswith("**Verdict: FAIL**")
    assert any("no pipeline report" in n for n in out["notes"])
    md = review.read_text(encoding="utf-8")
    assert f"- Netlist: `{netlist}`" in md
    assert re.search(r"^\| R1 \|.*\| stock \|$", md, re.M)
    assert "Library sources: stock 2." in md
    assert out["library_sources_error"] is None and out["safety_flags"] == []


def test_review_with_a_pipeline_report_never_touches_its_bytes(netlist):
    report = netlist.parent / "divider_pipeline_report.json"
    raw = (json.dumps(_passing_report(str(netlist)), indent=1) + "\n  \n").encode("utf-8")
    report.write_bytes(raw)
    stamp = 1_700_000_000
    os.utime(report, (stamp, stamp))
    rt.record_decision(str(netlist), **_decision(confidence="low"))
    rt.record_decision(str(netlist), **_decision(stage="routing", decision="Freerouting first",
                                                 confidence="high"))
    out = rt.review_report(str(netlist))
    assert out["ok"] is True, out
    assert out["pipeline_report"] == str(report)
    assert report.read_bytes() == raw and report.stat().st_mtime == stamp
    assert out["verdict"] == "**Verdict: PASS**: all 5 gates passed (G1-G5)."
    assert out["decisions"] == 2 and out["decision_errors"] == []
    md = Path(out["review"]).read_text(encoding="utf-8")
    decisions = _section(md, "Decisions")
    assert "Yageo RC0603FR-0710KL" in decisions and "Freerouting first" in decisions
    assert "### parts" in decisions and "### routing" in decisions


def test_review_fills_in_a_report_without_a_netlist_key(netlist):
    report = netlist.parent / "divider_pipeline_report.json"
    report.write_text(json.dumps(_passing_report(None)), encoding="utf-8")
    raw = report.read_bytes()
    out = rt.review_report(str(netlist))
    assert out["ok"] is True
    assert f"- Netlist: `{netlist}`" in Path(out["review"]).read_text(encoding="utf-8")
    assert report.read_bytes() == raw


def test_a_bad_decision_log_line_becomes_an_open_item(netlist):
    rt.record_decision(str(netlist), **_decision())
    with open(dec.decisions_path(netlist), "a", encoding="ascii") as f:
        f.write("not json\n")
    out = rt.review_report(str(netlist))
    assert out["ok"] is True and out["decisions"] == 1
    assert len(out["decision_errors"]) == 1 and ":2:" in out["decision_errors"][0]
    md = Path(out["review"]).read_text(encoding="utf-8")
    opens = _section(md, "Open items")
    assert "Fix the decision log" in opens
    assert out["open_items"] == len(re.findall(r"^\d+\. ", opens, re.M)) > 0


def test_library_sources_error_is_reported_not_raised(netlist):
    (netlist.parent / "sym-lib-table").write_text("(sym_lib_table (lib (name x)",
                                                  encoding="utf-8")
    out = rt.review_report(str(netlist))
    assert out["ok"] is True
    assert out["library_sources_error"]
    assert any("library sources" in n for n in out["notes"])
    md = Path(out["review"]).read_text(encoding="utf-8")
    assert "Library sources were not supplied to the report." in md


def test_an_analysis_crash_is_a_note(netlist, monkeypatch):
    def boom(design, symbols_dir=""):
        raise RuntimeError("symbol parser exploded")

    monkeypatch.setattr(design_report, "analyze", boom)
    out = rt.review_report(str(netlist))
    assert out["ok"] is True
    assert any("symbol parser exploded" in n for n in out["notes"])


def test_safety_flags_and_banner(netlist):
    design = json.loads(netlist.read_text(encoding="utf-8"))
    netlist.write_text(json.dumps({**design, "net_specs": {"IN": {"voltage": 230.0}}}),
                       encoding="utf-8")
    out = rt.review_report(str(netlist))
    assert out["ok"] is True
    assert [f["kind"] for f in out["safety_flags"]] == ["mains_hv"]
    assert out["safety_flags"][0]["reason"]
    md = Path(out["review"]).read_text(encoding="utf-8")
    assert md.startswith("> **HUMAN REVIEW REQUIRED**")


@pytest.mark.parametrize("content, needle", [
    (None, "cannot read the netlist"),
    ("{not json", "cannot read the netlist"),
    ("[1, 2]", "JSON object"),
])
def test_an_unreadable_netlist_is_ok_false(tmp_path, content, needle):
    path = tmp_path / "n.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    out = rt.review_report(str(path))
    assert out["ok"] is False and needle in out["reason"]
    assert not (tmp_path / "n_review.md").exists()


@pytest.mark.parametrize("content", ["{truncated", json.dumps({"gates": "G1"})])
def test_a_bad_pipeline_report_is_ok_false_and_left_alone(netlist, content):
    report = netlist.parent / "divider_pipeline_report.json"
    report.write_text(content, encoding="utf-8")
    raw = report.read_bytes()
    out = rt.review_report(str(netlist))
    assert out["ok"] is False and "divider_pipeline_report.json" in out["reason"]
    assert report.read_bytes() == raw
    assert not (netlist.parent / "divider_review.md").exists()


def test_a_report_that_crashes_the_renderer_is_ok_false(netlist):
    report = netlist.parent / "divider_pipeline_report.json"
    bad = {**_passing_report(str(netlist)), "autoroute": {"attempt": 1},
           "drc": {"errors": 0, "types": ["not", "a", "map"]}}
    report.write_text(json.dumps(bad), encoding="utf-8")
    raw = report.read_bytes()
    out = rt.review_report(str(netlist))
    assert out["ok"] is False and "AttributeError" in out["reason"]
    assert report.read_bytes() == raw
    assert not (netlist.parent / "divider_review.md").exists()


def test_an_unwritable_review_is_ok_false(netlist):
    (netlist.parent / "divider_review.md").mkdir()           # the review path is a folder
    out = rt.review_report(str(netlist))
    assert out["ok"] is False and "cannot write the review" in out["reason"]


def test_without_stock_libraries_parts_are_unknown_and_noted(netlist, monkeypatch):
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": None)
    monkeypatch.setattr(tc, "footprints_dir", lambda explicit="": None)
    out = rt.review_report(str(netlist))
    assert out["ok"] is True
    assert any("no stock symbol library directory" in n for n in out["notes"])
    assert any("no stock footprint library directory" in n for n in out["notes"])
    md = Path(out["review"]).read_text(encoding="utf-8")
    assert "Library sources: unknown 2." in md


def test_install_discovery_failure_is_a_note(netlist, monkeypatch):
    def broken(explicit=""):
        raise OSError("probe failed")

    monkeypatch.setattr(tc, "symbols_dir", broken)
    out = rt.review_report(str(netlist))
    assert out["ok"] is True
    assert any("stock libraries unavailable" in n and "probe failed" in n
               for n in out["notes"])


def test_review_report_needs_a_netlist_path():
    out = rt.review_report("")
    assert out["ok"] is False and "netlist" in out["reason"]
