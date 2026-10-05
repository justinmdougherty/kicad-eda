"""foreign_import + foreign_board: foreign designs through KiCad's own
importers (docs/ROADMAP_DECISIONS.md D4, F6; F46-03b).

Unit tier (no KiCad): every KiCad child is a REAL subprocess of a fake
kicad-cli (tests/fixtures/fake_kicad.py), including rc=0 runs that print
"Error:" lines or write nothing; the SWIG fallback runs the real
swig_load_foreign.py against a stub pcbnew (9.0.4 / 10.0.5, zones
configurable) or a faked child with chosen F46-03a diagnostics. Covers the
report (source block, importer_footprint_library, footprint_links), console
Error: capture, the board backend choice (`pcb import` first, SWIG only as
the fallback, never with a layer map, Eagle only on pcbnew 10+), the
inner-plane-loss blocker, unsettled probes, library conversion on the OLDEST
capable install (D0 dialect) and the GUI-only human steps.

Live tier (skips without KiCad / the nightly / $KICAD_EDA_FOREIGN_SAMPLES):
Eagle Qwiic_Button on the nightly and a truncated copy (rc 3, XML parsing
error); an Altium SchDoc with Error: lines at rc=0; the KiCad 9 SWIG importer
(Altium plane loss, EasyEDA zones on missing layers); D0 library conversion;
the EasyEDA Pro / .PrjPcb human steps.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import foreign_board as fb
import foreign_formats as ff
import foreign_import as fi
import kicad_env as env
from fixtures.fake_kicad import (EAGLE_HEAD, altium, configure_stub, eagle_brd, eagle_sch,
                                 easyeda_pro, easyeda_std, fake_installer, fake_swig,
                                 kicad9_install, log_cli_calls, nightly_install, prjpcb,
                                 read_calls, read_stub_calls, sample, sha256, stub_pcbnew_python,
                                 work_calls, write)

K11 = "kicad-cli 10.99.0-4025-gfake"
ECO1_ZONES = [{"layers": ["Eco1.User"]}, {"layers": ["Eco1.User"]},
              {"layers": ["Eco1.User"], "rule_area": True}, {"layers": ["F.Cu"], "net": "GND"}]
PLANE_LOSS = {"zones": 4, "nets": ["GND", "P1.8VA", "P1.8VD", "P3.3V"]}


@pytest.fixture(autouse=True)
def fresh_probe_cache():
    ff.clear_cache()
    yield
    ff.clear_cache()


@pytest.fixture
def cli_log(tmp_path, monkeypatch) -> Path:
    return log_cli_calls(tmp_path, monkeypatch)


@pytest.fixture
def fake_install(tmp_path, cli_log):
    """fake_install("k9" | "k10" | "k11", python=None) -> an install on that fake CLI."""
    return fake_installer(tmp_path)


@pytest.fixture
def stub_python(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """(python, pcbnew calls log): the venv Python with a stub pcbnew."""
    return stub_pcbnew_python(tmp_path, monkeypatch)


@pytest.fixture(scope="module")
def installs() -> list[env.KiCadInstall]:
    return env.discover()


@pytest.fixture(scope="module")
def nightly(installs) -> env.KiCadInstall:
    return nightly_install(installs)


@pytest.fixture(scope="module")
def kicad9(installs) -> env.KiCadInstall:
    return kicad9_install(installs)


# ---------------------------------------------------------------------------
# import_project on the fake nightly
# ---------------------------------------------------------------------------
def test_eagle_board_and_schematic_make_a_project(tmp_path, fake_install, cli_log):
    src = tmp_path / "src"
    brd, sch = eagle_brd(src, "FAKE_FOOTPRINTS=4"), eagle_sch(src, "FAKE_SYMBOLS=5")
    out = tmp_path / "out"
    res = fi.import_project([brd, sch], out, name="qb", installs=[fake_install("k11")])
    assert res["status"] == "ok" and res["ok"], res["errors"]
    assert Path(res["project"]) == out / "qb.kicad_pro" and (out / "qb.kicad_pro").is_file()
    assert Path(res["board"]) == out / "qb.kicad_pcb"
    assert res["schematics"] == [str(out / "qb.kicad_sch")]
    assert res["root_schematic"] == str(out / "qb.kicad_sch")
    assert res["statistics"]["board"]["footprints"] == 4
    assert res["statistics"]["schematics"][str(out / "qb.kicad_sch")]["symbols"] == 5
    assert res["layer_mapping"]["Top"]["kicad_layer"] == "F.Cu"
    assert res["source"] == {"kind": "eagle", "path": str(brd), "sha256": sha256(brd),
                             "tool": f"{K11} pcb import",
                             "files": {"Qwiic_Button.brd": sha256(brd),
                                       "Qwiic_Button.sch": sha256(sch)}}
    assert [s["role"] for s in res["sources"]] == ["board", "schematic"]
    assert res["sources"][1]["sha256"] == sha256(sch)
    assert res["sources"][1]["tool"] == f"{K11} sch import"
    assert res["importer_footprint_library"] == str(out / "Qwiic_Button-import-fps.pretty")
    assert res["importer_footprint_library_note"] is None
    assert res["board_format_version"] == 20260901 and res["d0_dialect"] is False
    assert any("20260901" in w and "D0" in w for w in res["warnings"])
    assert res["footprint_links"] == {"board": ["Qwiic_Button-import-fps"], "schematic": ["qb"],
                                      "consistent": False, "counts": {"empty": 0, "unqualified": 0}}
    assert any("Qwiic_Button-import-fps" in w for w in res["warnings"])
    assert res["libraries"]["footprints"][0]["name"] == "Qwiic_Button-import-fps"
    assert res["libraries"]["footprints"][0]["import_cache"] is True
    assert res["libraries"]["symbols"][0]["name"] == "qb-eagle-import"
    assert res["errors"] == [] and res["human_steps"] == [] and res["hierarchy"] is None
    (pcb_call,) = work_calls(cli_log, "pcb", "import")
    assert pcb_call[pcb_call.index("--format") + 1] == "eagle"
    assert pcb_call[pcb_call.index("--report-format") + 1] == "json"
    (sch_call,) = work_calls(cli_log, "sch", "import")
    assert sch_call[sch_call.index("--format") + 1] == "eagle"
    assert work_calls(cli_log, "import") == []          # per-file imports, with reports
    saved = json.loads(Path(res["report_path"]).read_text(encoding="utf-8"))
    assert saved["board"] == res["board"] and saved["source"] == res["source"]
    for step in res["steps"]:
        assert Path(step["log"]).is_file() and step["rc"] == 0 and step["status"] == "ok"


def test_console_errors_with_rc0_and_clean_report_degrade_the_import(tmp_path, fake_install):
    sch = altium(tmp_path / "src", "power.SchDoc", "FAKE_ERRORS=3 FAKE_SYMBOLS=6")
    res = fi.import_project([sch], tmp_path / "out", installs=[fake_install("k11")])
    (step,) = res["steps"]
    assert step["rc"] == 0 and step["report"]["errors"] == []      # rc and report say fine
    assert step["status"] == "degraded" and len(step["console_errors"]) == 3
    assert res["status"] == "degraded" and not res["ok"]
    assert res["errors"] == [f"power.SchDoc: No library symbol found for schematic symbol "
                             f"'R{i}P 10k'." for i in range(3)]
    assert "Error: No library symbol" in Path(step["log"]).read_text(encoding="utf-8")


def test_report_errors_and_count_mismatch(tmp_path, fake_install):
    brd = eagle_brd(tmp_path / "src", "FAKE_REPORT_ERROR FAKE_FOOTPRINTS=3 FAKE_CLAIM=5")
    res = fi.import_project([brd], tmp_path / "out", installs=[fake_install("k11")])
    assert res["status"] == "degraded"
    assert "Qwiic_Button.brd: report error" in res["errors"]
    assert any("report says 5 footprints" in w and "has 3" in w for w in res["warnings"])


def test_rc0_without_an_output_file_is_a_failure(tmp_path, fake_install):
    brd = eagle_brd(tmp_path / "src", "FAKE_NO_OUTPUT")
    res = fi.import_project([brd], tmp_path / "out", installs=[fake_install("k11")])
    (step,) = res["steps"]
    assert step["rc"] == 0 and step["status"] == "failed"
    assert res["status"] == "failed" and res["board"] is None and res["project"] is None
    assert any("was not written" in e for e in res["errors"])
    assert res["importer_footprint_library"] is None
    assert "no board was imported" in res["importer_footprint_library_note"]


def test_multi_schdoc_altium_records_the_hierarchy_to_assemble(tmp_path, fake_install):
    src = tmp_path / "src"
    files = [altium(src, "eDP.PcbDoc"), altium(src, "power.SchDoc"),
             altium(src, "it6251_core.SchDoc")]
    out = tmp_path / "out"
    res = fi.import_project(files, out, installs=[fake_install("k11")])
    assert res["status"] == "ok", res["errors"]
    assert res["board"] == str(out / "eDP.kicad_pcb")
    assert res["schematics"] == [str(out / "power.kicad_sch"), str(out / "it6251_core.kicad_sch")]
    assert res["root_schematic"] is None
    assert res["hierarchy"]["assembly_required"] is True
    assert res["hierarchy"]["sheets"] == res["schematics"]
    assert sum(1 for s in res["steps"] if s["step"] == "sch import") == 2
    assert any("hierarchy" in w for w in res["warnings"])
    assert res["source"]["path"] == str(files[0])
    assert res["source"]["files"] == {f.name: sha256(f) for f in files}


def test_source_block_without_a_board_names_the_first_schematic(tmp_path, fake_install):
    src = tmp_path / "src"
    proj, power, core = prjpcb(src), altium(src, "power.SchDoc"), altium(src, "core.SchDoc")
    res = fi.import_project([proj, power, core], tmp_path / "out", installs=[fake_install("k11")])
    assert res["source"]["kind"] == "altium" and res["source"]["path"] == str(power)
    assert res["source"]["sha256"] == sha256(power)
    assert res["source"]["tool"] == f"{K11} sch import"
    assert res["source"]["files"] == {p.name: sha256(p) for p in (proj, power, core)}
    assert res["importer_footprint_library"] is None
    assert "no board was imported" in res["importer_footprint_library_note"]


def test_existing_outputs_are_never_overwritten(tmp_path, fake_install, cli_log):
    out = tmp_path / "out"
    write(out / "fp-lib-table", "(fp_lib_table)")
    with pytest.raises(FileExistsError, match="fp-lib-table"):
        fi.import_project([eagle_brd(tmp_path / "src")], out, installs=[fake_install("k11")])
    assert read_calls(cli_log) == []                         # refused before any probe or import


def test_bad_requests_fail_fast(tmp_path, fake_install):
    k11 = [fake_install("k11")]
    src = tmp_path / "src"
    with pytest.raises(ValueError, match="no input"):
        fi.import_project([], tmp_path / "o", installs=k11)
    with pytest.raises(ValueError, match="one board"):
        fi.import_project([eagle_brd(src), altium(src, "x.PcbDoc")], tmp_path / "o", installs=k11)
    with pytest.raises(ValueError, match="convert_library"):
        fi.import_project([altium(src, "esp.PcbLib")], tmp_path / "o", installs=k11)
    with pytest.raises(ValueError, match="the SWIG importer loads only"):
        fi.import_project([easyeda_pro(src, "b.zip", sheets=False)], tmp_path / "o",
                          board_backend="swig", installs=k11)
    with pytest.raises(ValueError, match="board_backend"):
        fi.import_project([eagle_brd(src)], tmp_path / "o", board_backend="gui", installs=k11)


def test_duplicate_inputs_are_refused_before_any_probe(tmp_path, fake_install, cli_log,
                                                       monkeypatch):
    sch = altium(tmp_path / "src", "power.SchDoc")
    monkeypatch.chdir(tmp_path)
    same = [sch, Path("src") / "power.SchDoc", tmp_path / "src" / ".." / "src" / "power.SchDoc"]
    if os.name == "nt":
        same.append(Path(str(sch).upper()))
    for other in same:
        with pytest.raises(ValueError, match=r"power\.SchDoc is given more than once"):
            fi.import_project([sch, other], tmp_path / "out", installs=[fake_install("k11")])
    assert read_calls(cli_log) == []


def test_two_sheets_with_one_output_name_are_refused(tmp_path, fake_install, cli_log):
    a, b = altium(tmp_path / "a", "power.SchDoc"), altium(tmp_path / "b", "Power.SchDoc")
    with pytest.raises(ValueError, match="same file"):
        fi.import_project([a, b], tmp_path / "out", installs=[fake_install("k11")])
    assert read_calls(cli_log) == []


def test_layer_map_needs_a_cli_that_takes_it(tmp_path, fake_install, cli_log):
    brd = eagle_brd(tmp_path / "src")
    lm = write(tmp_path / "map.json", '{"Top": "F.Cu"}')
    with pytest.raises(ValueError, match="--layer-map"):
        fi.import_project([brd], tmp_path / "o10", layer_map=lm, installs=[fake_install("k10")])
    with pytest.raises(ValueError, match="no board"):
        fi.import_project([eagle_sch(tmp_path / "src")], tmp_path / "os", layer_map=lm,
                          installs=[fake_install("k11")])
    with pytest.raises(ValueError, match="SWIG importer takes no layer map"):
        fi.import_project([altium(tmp_path / "src", "x.PcbDoc")], tmp_path / "ow",
                          layer_map=lm, board_backend="swig", installs=[fake_install("k11")])
    with pytest.raises(FileNotFoundError, match="layer_map"):
        fi.import_project([brd], tmp_path / "om", layer_map=str(tmp_path / "missing.json"),
                          installs=[fake_install("k11")])
    res = fi.import_project([brd], tmp_path / "o11", layer_map=lm, installs=[fake_install("k11")])
    assert res["status"] == "ok"
    (pcb_call,) = work_calls(cli_log, "pcb", "import")
    assert pcb_call[0] == "k11" and pcb_call[pcb_call.index("--layer-map") + 1] == str(lm)


def test_a_timed_out_import_child_fails_the_step(tmp_path, fake_install, monkeypatch):
    real_run = ff.run_child

    def hang_imports(argv, timeout_s, cwd=None):
        if "--help" in argv:
            return real_run(argv, timeout_s, cwd)
        return env.ProbeResult(env.TRANSIENT_RC, "", f"{argv[0]}: timed out after "
                                                     f"{timeout_s:g} s")

    monkeypatch.setattr(ff, "run_child", hang_imports)
    res = fi.import_project([eagle_brd(tmp_path / "src")], tmp_path / "out",
                            installs=[fake_install("k11")])
    (step,) = res["steps"]
    assert step["status"] == "failed" and res["status"] == "failed"
    assert any("timed out after 600 s" in e for e in step["errors"])


def test_kicad10_imports_the_board_and_asks_a_human_for_the_schematic(tmp_path, fake_install):
    src = tmp_path / "src"
    out = tmp_path / "out"
    inputs = [eagle_brd(src), eagle_sch(src)]
    res = fi.import_project(inputs, out, installs=[fake_install("k10")])
    assert res["status"] == "incomplete" and res["board"] and res["schematics"] == []
    assert sorted(src.iterdir()) == sorted(inputs)      # pcb import read a copy: no .kicad_dru
    assert res["steps"][0]["importer_side_files"] == ["Qwiic_Button.kicad_dru"]
    assert res["steps"][0]["argv"][-1] != str(inputs[0])
    (step,) = res["human_steps"]
    assert step["role"] == "schematic" and "sch import" in step["why"]
    assert "File > Import > Non-KiCad Schematic" in step["action"]
    assert "unsettled" not in step["why"]
    assert res["board_format_version"] == 20260206
    assert res["importer_footprint_library"] is None              # 10.0.5 writes none
    note = res["importer_footprint_library_note"]
    assert "kicad-cli 10.0.5 pcb import wrote no footprint library" in note
    assert res["libraries"]["footprints"] == [] and not (out / "fp-lib-table").exists()


def test_footprint_links_are_unknown_when_schematic_fields_are_empty_or_unqualified(
        tmp_path, fake_install):
    src = tmp_path / "src"
    brd, k11 = eagle_brd(src), [fake_install("k11")]
    res = fi.import_project([brd, eagle_sch(src, "FAKE_SYMBOLS=5 FAKE_FP_EMPTY=2 FAKE_FP_BARE=3")],
                            tmp_path / "o1", installs=k11)
    links = res["footprint_links"]
    assert links["consistent"] is None and links["schematic"] == []
    assert links["counts"] == {"empty": 2, "unqualified": 3}     # lib_symbols copies not counted
    assert any("2 empty and 3 unqualified" in w and "D4" in w for w in res["warnings"])
    # a qualified nickname the board does not use stays a definite mismatch
    res = fi.import_project([brd, eagle_sch(src / "b", "FAKE_SYMBOLS=3 FAKE_FP_EMPTY=1")],
                            tmp_path / "o2", installs=k11)
    assert res["footprint_links"]["consistent"] is False
    assert res["footprint_links"]["counts"] == {"empty": 1, "unqualified": 0}
    # every field names the board's own library: consistent
    res = fi.import_project([brd, eagle_sch(src / "c")], tmp_path / "o3",
                            name="Qwiic_Button-import-fps", installs=k11)
    assert res["footprint_links"]["consistent"] is True


def test_without_report_format_the_counts_come_from_the_files(tmp_path, fake_install,
                                                              monkeypatch):
    real = ff.capabilities
    monkeypatch.setattr(ff, "capabilities", lambda i: replace(real(i), pcb_report_json=False))
    res = fi.import_project([eagle_brd(tmp_path / "src", "FAKE_FOOTPRINTS=6")],
                            tmp_path / "out", installs=[fake_install("k10")])
    (step,) = res["steps"]
    assert "--report-format" not in step["argv"] and step["report"] is None
    assert step["statistics"] == {"footprints": 6} and res["status"] == "ok"
    assert any("has no --report-format" in w for w in res["warnings"])


def test_malformed_report_and_unreadable_library_table_are_warnings(tmp_path, fake_install):
    brd = eagle_brd(tmp_path / "src", "FAKE_BAD_REPORT FAKE_BAD_TABLE")
    res = fi.import_project(str(brd), tmp_path / "out", installs=[fake_install("k11")])
    (step,) = res["steps"]
    assert step["report"] is None and step["statistics"] == {"footprints": 3}
    assert any("is not JSON" in w for w in res["warnings"])
    assert any("unreadable fp-lib-table" in w for w in res["warnings"])
    assert res["libraries"]["footprints"] == [] and res["status"] == "ok"


def test_unsettled_probes_are_warnings_and_named_in_human_steps(tmp_path, fake_install,
                                                                monkeypatch):
    real_run = ff.run_child

    def flaky(argv, timeout_s, cwd=None):
        if "--help" in argv and "import" in argv:
            return env.ProbeResult(env.TRANSIENT_RC, "", f"{argv[0]}: timed out after 30 s")
        return real_run(argv, timeout_s, cwd)

    monkeypatch.setattr(ff, "run_child", flaky)
    src = tmp_path / "src"
    res = fi.import_project([eagle_brd(src), eagle_sch(src)], tmp_path / "out",
                            installs=[fake_install("k11")])
    assert res["status"] == "incomplete" and res["steps"] == []
    unsettled = [w for w in res["warnings"] if "capability probe unsettled" in w]
    assert len(unsettled) == 3 and all("timed out after 30 s" in w for w in unsettled)
    board, schematic = res["human_steps"]
    assert board["id"].startswith("no_board_importer") and schematic["role"] == "schematic"
    for step in (board, schematic):
        assert "capability probe unsettled" in step["why"] and "re-run" in step["why"]


# ---------------------------------------------------------------------------
# Board backend: `pcb import` first, the SWIG importer as the fallback
# ---------------------------------------------------------------------------
def test_auto_prefers_pcb_import_over_the_swig_importer(tmp_path, fake_install, stub_python):
    python, calls = stub_python
    res = fi.import_project([altium(tmp_path / "src", "eDP.PcbDoc")], tmp_path / "out",
                            installs=[fake_install("k9", python=python), fake_install("k10")])
    (step,) = res["steps"]
    assert step["step"] == "pcb import" and step["tool"] == "kicad-cli 10.0.5 pcb import"
    assert res["status"] == "ok" and read_stub_calls(calls) == ""
    assert res["source"]["tool"] == "kicad-cli 10.0.5 pcb import"


def test_auto_falls_back_to_swig_without_a_board_cli(tmp_path, fake_install, stub_python,
                                                     monkeypatch):
    python, calls = stub_python
    configure_stub(monkeypatch, copper=2, zones=[{"layers": ["In1.Cu"], "net": "GND"},
                                                 {"layers": ["In2.Cu"]}, {"layers": ["F.Cu"]}])
    out = tmp_path / "out"
    res = fi.import_project([easyeda_std(tmp_path / "src", "watch.json", "3")], out,
                            installs=[fake_install("k9", python=python)])
    assert res["status"] == "ok", res["errors"]
    (step,) = res["steps"]
    assert step["step"] == "swig LoadBoard" and step["statistics"]["footprints"] == 5
    assert step["tool"] == "pcbnew 9.0.4 LoadBoard(EASYEDA)"
    assert res["board"] == str(out / "watch.kicad_pcb") and res["d0_dialect"] is True
    assert res["source"]["tool"] == step["tool"]
    assert any("2 copper zone(s) on layers this 2-layer board does not have "
               "(In1.Cu: 1, In2.Cu: 1)" in w for w in res["warnings"])
    assert any("no KiCad equivalent" in w for w in res["warnings"])
    assert res["importer_footprint_library"] is None
    assert "SWIG" in res["importer_footprint_library_note"]
    assert "quiet_first=True" in read_stub_calls(calls)


def test_auto_swig_fallback_with_inner_plane_loss_is_a_blocker(tmp_path, fake_install,
                                                               stub_python, monkeypatch):
    python, _ = stub_python
    configure_stub(monkeypatch, copper=4, zones=ECO1_ZONES)
    out = tmp_path / "out"
    res = fi.import_project([altium(tmp_path / "src", "eDP.PcbDoc")], out,
                            installs=[fake_install("k9", python=python)])
    (step,) = res["steps"]
    assert step["step"] == "swig LoadBoard" and step["rc"] == 0 and step["status"] == "failed"
    assert step["result"]["inner_plane_loss"]["zones"] == 2        # rule areas do not count
    assert any(e.startswith("inner plane loss") for e in step["errors"])
    assert res["status"] == "failed" and not res["ok"] and res["board"] is None
    (human,) = res["human_steps"]
    assert human["id"] == "inner_plane_loss:eDP.PcbDoc" and human["blocking"] is True
    assert human["action"].startswith("Import with KiCad 10+")
    assert human["expected_output"] == str(out / "eDP.kicad_pcb")
    assert not (out / "eDP.kicad_pcb").exists() and Path(human["lossy_output"]).is_file()
    assert step["lossy_output"] == human["lossy_output"] and step["output"] is None


@pytest.mark.parametrize(("backend", "status"), [("auto", "failed"), ("swig", "degraded")])
def test_inner_plane_loss_blocks_auto_and_degrades_an_explicit_swig_import(
        tmp_path, fake_install, monkeypatch, backend, status):
    fake_swig(monkeypatch, inner_plane_loss=PLANE_LOSS)
    out = tmp_path / "out"
    res = fi.import_project([altium(tmp_path / "src", "eDP.PcbDoc")], out, board_backend=backend,
                            installs=[fake_install("k9", python=Path(sys.executable))])
    (step,) = res["steps"]
    message = ("inner plane loss (pcbnew 9.0.4 LoadBoard(ALTIUM_DESIGNER)): 4 Altium "
               "internal-plane zone(s) moved to Eco1.User (no copper); plane net(s) GND, "
               "P1.8VA, P1.8VD, P3.3V have no zone on any In*.Cu")
    assert message in step["errors"] and step["status"] == status == res["status"]
    assert f"eDP.PcbDoc: {message}" in res["errors"]
    if backend == "auto":
        assert res["board"] is None
        assert [h["id"] for h in res["human_steps"]] == ["inner_plane_loss:eDP.PcbDoc"]
    else:
        assert res["board"] == str(out / "eDP.kicad_pcb") and res["human_steps"] == []


def test_a_failed_cli_import_then_a_lossy_swig_fallback_still_blocks(tmp_path, fake_install,
                                                                     stub_python):
    python, _ = stub_python
    res = fi.import_project([altium(tmp_path / "src", "eDP.PcbDoc", "FAKE_CRASH")],
                            tmp_path / "out",
                            installs=[fake_install("k11"), fake_install("k9", python=python)])
    (step,) = res["steps"]
    assert step["step"] == "swig LoadBoard" and step["status"] == "failed"
    assert step["fallback_from"]["rc"] == 127
    assert any("could not be ruled out" in e for e in step["errors"])    # fail closed
    assert res["status"] == "failed" and res["board"] is None
    assert res["human_steps"][0]["id"] == "inner_plane_loss:eDP.PcbDoc"


def test_explicit_swig_altium_on_pcbnew9_is_degraded_not_blocked(tmp_path, fake_install,
                                                                 stub_python, monkeypatch):
    python, _ = stub_python
    configure_stub(monkeypatch, copper=4, zones=ECO1_ZONES)
    res = fi.import_project([altium(tmp_path / "src", "eDP.PcbDoc")], tmp_path / "out",
                            board_backend="swig",
                            installs=[fake_install("k11"), fake_install("k9", python=python)])
    (step,) = res["steps"]
    assert step["step"] == "swig LoadBoard" and step["status"] == "degraded"
    assert res["status"] == "degraded" and res["board"] and res["human_steps"] == []
    assert any(e.startswith("eDP.PcbDoc: inner plane loss") for e in res["errors"])


def test_swig_load_errors_fail_once_not_twice(tmp_path, fake_install, stub_python):
    """The script's JSON result line is not console output: its "Error:" text is
    the step's failure, not also a console error."""
    python, _ = stub_python
    res = fi.import_project([altium(tmp_path / "src", "bad.PcbDoc", "FAKE_RAISE")],
                            tmp_path / "out", installs=[fake_install("k9", python=python)])
    (step,) = res["steps"]
    assert step["status"] == "failed" and step["rc"] == 1 and step["console_errors"] == []
    assert step["errors"] == ["LoadBoard(ALTIUM_DESIGNER) raised OSError: Error: cannot read "
                              "the board file"]
    assert res["status"] == "failed" and res["board"] is None and res["project"] is None
    assert res["human_steps"] == []


def test_swig_rc0_without_an_output_file_is_a_failure(tmp_path, fake_install, monkeypatch):
    fake_swig(monkeypatch, write_board=False)
    res = fi.import_project([easyeda_std(tmp_path / "src", "w.json", "3")], tmp_path / "out",
                            installs=[fake_install("k9", python=Path(sys.executable))])
    (step,) = res["steps"]
    assert step["rc"] == 0 and step["status"] == "failed"
    assert step["errors"] == ["w.kicad_pcb was not written"] and res["status"] == "failed"


def test_cli_crash_falls_back_to_swig(tmp_path, fake_install, stub_python):
    python, _ = stub_python
    src = easyeda_std(tmp_path / "src", "watch.json", "3", "FAKE_CRASH")
    res = fi.import_project([src], tmp_path / "out",
                            installs=[fake_install("k11"), fake_install("k9", python=python)])
    (step,) = res["steps"]
    assert step["step"] == "swig LoadBoard" and step["status"] == "ok"
    assert step["fallback_from"]["status"] == "failed" and step["fallback_from"]["rc"] == 127
    assert "std::bad_function_call" in step["fallback_from"]["console_errors"][0]
    assert res["status"] == "ok" and any("fell back" in w for w in res["warnings"])


def test_a_layer_map_never_falls_back_to_swig(tmp_path, fake_install, stub_python):
    python, calls = stub_python
    lm = write(tmp_path / "map.json", '{"Top": "F.Cu"}')
    res = fi.import_project([altium(tmp_path / "src", "eDP.PcbDoc", "FAKE_CRASH")],
                            tmp_path / "out", layer_map=lm,
                            installs=[fake_install("k11"), fake_install("k9", python=python)])
    (step,) = res["steps"]
    assert step["step"] == "pcb import" and step["status"] == "failed"
    assert "fallback_from" not in step and read_stub_calls(calls) == ""
    assert "layer map not applied: the SWIG importer takes none" in step["errors"]
    assert res["status"] == "failed"


def test_eagle_goes_through_swig_only_on_pcbnew10(tmp_path, fake_install, stub_python,
                                                  monkeypatch):
    python, calls = stub_python
    brd = eagle_brd(tmp_path / "src")
    for backend in ("auto", "swig"):
        res = fi.import_project([brd], tmp_path / backend, board_backend=backend,
                                installs=[fake_install("k9", python=python)])
        assert res["status"] == "incomplete" and res["steps"] == []
        (step,) = res["human_steps"]
        assert step["id"] == "no_board_importer:Qwiic_Button.brd" and step["role"] == "board"
        assert "pcbnew 10+ Python (LoadBoard(EAGLE) returns None on KiCad 9)" in step["why"]
    assert read_stub_calls(calls) == ""
    configure_stub(monkeypatch, version="10.0.5")
    res = fi.import_project([brd], tmp_path / "k10", board_backend="swig",
                            installs=[fake_install("k10", python=python)])
    (step,) = res["steps"]
    assert step["tool"] == "pcbnew 10.0.5 LoadBoard(EAGLE)" and step["status"] == "ok"
    assert res["board_format_version"] == 20260206 and res["d0_dialect"] is False
    assert "LoadBoard Qwiic_Button.brd 8 quiet_first=True" in read_stub_calls(calls)


def test_an_unstaged_board_is_imported_in_place_with_a_warning(tmp_path, fake_install,
                                                               monkeypatch):
    def no_copy(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(fb.shutil, "copyfile", no_copy)
    brd = eagle_brd(tmp_path / "src")
    res = fi.import_project([brd], tmp_path / "out", installs=[fake_install("k10")])
    assert res["status"] == "ok" and res["steps"][0]["argv"][-1] == str(brd)
    assert any("could not stage a copy" in w and "disk full" in w for w in res["warnings"])


def test_file_helpers_fail_soft(tmp_path, monkeypatch):
    assert fb.read_text(tmp_path / "missing.kicad_pcb") == ""
    junk = write(tmp_path / "junk.kicad_pcb", "(kicad_sch)")
    assert fb.output_problem(junk, "kicad_pcb") == "junk.kicad_pcb is not a (kicad_pcb ...) file"
    assert fb.swig_install([], ff.classify(easyeda_pro(tmp_path, "b.zip", sheets=False))) is None
    assert fi._placed_text("(kicad_sch (symbol))") == "(kicad_sch (symbol))"
    assert fi._placed_text('(kicad_sch (lib_symbols (symbol ")"') == "(kicad_sch "
    for partial in (write(tmp_path / "p.pretty" / "a.kicad_mod", "x").parent,
                    write(tmp_path / "p.kicad_sym", "x"), tmp_path / "never_written"):
        fi._remove(partial)
        assert not partial.exists()
    monkeypatch.setattr(fb.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("locked")))
    step = {"created": [], "console_errors": []}
    board, pcbdoc = write(tmp_path / "x.kicad_pcb", "(kicad_pcb)"), altium(tmp_path, "x.PcbDoc")
    failed, ask = fb._plane_loss_blocker(ff.classify(pcbdoc), step, board, "m")
    assert failed["status"] == "failed" and ask["lossy_output"] == str(board) and board.is_file()
    assert "could not be set aside (locked)" in ask["why"]


@pytest.mark.parametrize(("backend", "flavor", "why"), [
    ("cli", "k9", "board_backend='cli' but no installed kicad-cli has `pcb import`"),
    ("swig", "k11", "board_backend='swig' but no KiCad install has a pcbnew Python"),
    ("auto", "k9", "no installed kicad-cli has `pcb import` (KiCad 10+) and no install has "
                   "a pcbnew Python for the SWIG importer")])
def test_no_board_importer_says_why(tmp_path, fake_install, backend, flavor, why):
    res = fi.import_project([altium(tmp_path / "src", "eDP.PcbDoc")], tmp_path / "out",
                            board_backend=backend, installs=[fake_install(flavor)])
    (step,) = res["human_steps"]
    assert step["id"] == "no_board_importer:eDP.PcbDoc" and why in step["why"]
    assert step["action"].startswith("Install KiCad 10 or newer")
    assert res["status"] == "incomplete" and res["steps"] == []


# ---------------------------------------------------------------------------
# GUI-only cases -> human_step
# ---------------------------------------------------------------------------
HUMAN_STEP_KEYS = {"id", "input", "role", "format", "why", "action", "expected_output",
                   "blocking"}


def test_altium_project_is_a_human_step(tmp_path, fake_install, cli_log):
    proj = prjpcb(tmp_path / "src")
    res = fi.import_project([proj], tmp_path / "out", installs=[fake_install("k11")])
    assert res["status"] == "incomplete" and not res["ok"] and res["steps"] == []
    (step,) = res["human_steps"]
    assert HUMAN_STEP_KEYS <= set(step) and step["blocking"] is True
    assert step["id"] == "gui_only_project:eDP_adapter_dvt1.PrjPcb"
    assert step["documents"] == ["power.SchDoc", "it6251_core.SchDoc", "eDP_adapter_dvt1.PcbDoc"]
    assert "Import Non-KiCad Project" in step["action"]
    assert not any(work_calls(cli_log, w) for w in ("import", "pcb", "sch"))
    assert res["source"]["path"] == str(proj) and "tool" not in res["source"]


def test_easyeda_pro_imports_sheets_and_asks_a_human_for_the_board(tmp_path, fake_install):
    out = tmp_path / "out"
    res = fi.import_project([easyeda_pro(tmp_path / "src")], out, installs=[fake_install("k11")])
    assert res["status"] == "incomplete"
    assert res["schematics"] == [str(out / "yuzuki.kicad_sch"), str(out / "1_POWER.kicad_sch")]
    assert res["root_schematic"] == str(out / "yuzuki.kicad_sch")
    assert res["hierarchy"] is None                     # the importer wrote the sub-sheets
    (step,) = res["human_steps"]
    assert HUMAN_STEP_KEYS <= set(step) and step["role"] == "board"
    assert step["expected_output"] == str(out / "yuzuki.kicad_pcb")
    assert "Non-KiCad Board File" in step["action"] and "bad_function_call" in step["why"]
    assert res["project"] == str(out / "yuzuki.kicad_pro")


def test_board_only_easyeda_pro_zip_is_a_human_step(tmp_path, fake_install, cli_log):
    out = tmp_path / "out"
    res = fi.import_project([easyeda_pro(tmp_path / "src", "watch.zip", sheets=False)], out,
                            installs=[fake_install("k11")])
    (step,) = res["human_steps"]
    assert step["id"] == "gui_only_board:watch.zip" and "bad_function_call" in step["why"]
    assert step["expected_output"] == str(out / "watch.kicad_pcb")
    assert res["status"] == "incomplete" and res["steps"] == [] and res["project"] is None
    assert work_calls(cli_log, "pcb", "import") == []


# ---------------------------------------------------------------------------
# Library conversion (oldest capable install first: the D0 dialect)
# ---------------------------------------------------------------------------
def test_library_conversion_prefers_kicad9(tmp_path, fake_install, cli_log):
    lbr = write(tmp_path / "mem.lbr", EAGLE_HEAD + "<library/>FAKE_ITEMS=3</drawing></eagle>")
    out = tmp_path / "libs"
    res = fi.convert_library(lbr, out, name="SF_mem",
                             installs=[fake_install("k11"), fake_install("k9")])
    assert res["status"] == "ok" and res["ok"], res["errors"]
    fp, sym = res["libraries"]
    assert (fp["kind"], fp["path"], fp["count"]) == ("footprint", str(out / "SF_mem.pretty"), 3)
    assert (fp["format_version"], fp["d0_dialect"], fp["tool"]) == \
        (20241229, True, "kicad-cli 9.0.4")
    assert (sym["kind"], sym["path"], sym["count"]) == ("symbol", str(out / "SF_mem.kicad_sym"), 3)
    assert (sym["format_version"], sym["d0_dialect"]) == (20241209, True)
    assert res["source"]["kind"] == "eagle" and res["source"]["sha256"] == sha256(lbr)
    upgrades = work_calls(cli_log, "fp", "upgrade") + work_calls(cli_log, "sym", "upgrade")
    assert len(upgrades) == 2 and {c[0] for c in upgrades} == {"k9"}
    assert all("--force" in c for c in upgrades)


def test_library_conversion_falls_back_to_a_newer_dialect(tmp_path, fake_install):
    lib = write(tmp_path / "LS2K.elibz2", "PK fake")
    res = fi.convert_library(lib, tmp_path / "libs",
                             installs=[fake_install("k9"), fake_install("k11")])
    (fp,) = res["libraries"]
    assert fp["ok"] and fp["tool"] == K11
    assert fp["format_version"] == 20260901 and fp["d0_dialect"] is False
    assert [a["tool"] for a in fp["attempts"]] == ["kicad-cli 9.0.4", K11]   # oldest first
    assert fp["attempts"][0]["errors"] == ["Unable to convert library"]
    assert res["status"] == "degraded" and any("D0" in w for w in res["warnings"])
    assert fp["path"] == str(tmp_path / "libs" / "LS2K.pretty") and fp["count"] == 2


def test_library_conversion_fails_when_every_converter_fails(tmp_path, fake_install):
    lib = write(tmp_path / "LS2K.elibz2", "PK fake")
    res = fi.convert_library(lib, tmp_path / "libs", installs=[fake_install("k9")])
    (fp,) = res["libraries"]
    assert not fp["ok"] and res["status"] == "failed" and not res["ok"]
    assert res["errors"] == ["kicad-cli 9.0.4: exit code 2: Unable to convert library"]
    assert not (tmp_path / "libs" / "LS2K.pretty").exists()


def test_library_conversion_refusals(tmp_path, fake_install):
    lbr = write(tmp_path / "mem.lbr", EAGLE_HEAD + "<library/></drawing></eagle>")
    write(tmp_path / "libs" / "mem.kicad_sym", "(kicad_symbol_lib)")
    with pytest.raises(FileExistsError, match="mem.kicad_sym"):
        fi.convert_library(lbr, tmp_path / "libs", installs=[fake_install("k9")])
    with pytest.raises(ff.ForeignFormatError, match="import_project"):
        fi.convert_library(eagle_brd(tmp_path), tmp_path / "x", installs=[fake_install("k9")])
    res = fi.convert_library(lbr, tmp_path / "none", installs=[])
    assert res["status"] == "failed" and any("fp upgrade" in e for e in res["errors"])


# ---------------------------------------------------------------------------
# Live tier: real KiCad installs and the sample files on disk
# ---------------------------------------------------------------------------
def test_live_eagle_qwiic_button_on_the_nightly(nightly, tmp_path):
    brd, sch = sample("eagle", "Qwiic_Button.brd"), sample("eagle", "Qwiic_Button.sch")
    out = tmp_path / "qb"
    res = fi.import_project([brd, sch], out, name="qb", installs=[nightly])
    assert res["status"] == "ok", (res["errors"], res["steps"])
    assert Path(res["project"]).is_file() and Path(res["board"]).is_file()
    assert res["root_schematic"] == str(out / "qb.kicad_sch")
    board = res["statistics"]["board"]
    assert (board["footprints"], board["tracks"], board["vias"], board["zones"]) == (48, 193, 38, 2)
    assert res["statistics"]["schematics"][str(out / "qb.kicad_sch")]["symbols"] == 53
    assert res["layer_mapping"]["Top"]["kicad_layer"] == "F.Cu"
    assert res["layer_mapping"]["Dimension"]["kicad_layer"] == "Edge.Cuts"
    assert res["source"]["kind"] == "eagle" and res["source"]["sha256"] == sha256(brd)
    assert res["source"]["tool"] == f"kicad-cli {nightly.version} pcb import"
    assert res["source"]["files"] == {brd.name: sha256(brd), sch.name: sha256(sch)}
    lib = Path(res["importer_footprint_library"])
    assert lib == out / "Qwiic_Button-import-fps.pretty" and any(lib.glob("*.kicad_mod"))
    assert res["footprint_links"]["consistent"] is False      # the importer's nickname quirk
    for step in res["steps"]:                                  # every console line was scanned
        log = Path(step["log"]).read_text(encoding="utf-8")
        assert step["console_errors"] == list(ff.scan_console(log).errors)
    assert res["errors"] == []


def test_live_truncated_eagle_board_fails_with_its_xml_error(nightly, tmp_path):
    full = sample("eagle", "Qwiic_Button.brd").read_bytes()
    trunc = write(tmp_path / "src" / "Qwiic_Button.brd", "")
    trunc.write_bytes(full[:20000])                           # cut mid-file: unclosed XML
    res = fi.import_project([trunc], tmp_path / "out", installs=[nightly])
    (step,) = res["steps"]
    assert step["rc"] == 3 and step["status"] == "failed"
    assert any("XML parsing error" in e for e in step["console_errors"]), step["console_errors"]
    assert any("XML parsing error" in e for e in res["errors"])
    assert res["status"] == "failed" and res["board"] is None and res["project"] is None


def test_live_altium_schdoc_errors_are_captured_despite_rc0(nightly, tmp_path):
    sch = sample("foreign", "altium", "power.SchDoc")
    res = fi.import_project([sch], tmp_path / "alt", installs=[nightly])
    (step,) = res["steps"]
    assert step["rc"] == 0 and step["report"]["errors"] == []
    assert len(step["console_errors"]) >= 40 and step["status"] == "degraded"
    assert any("No library symbol found" in e for e in res["errors"])
    assert res["status"] == "degraded"
    assert res["footprint_links"] is None                     # no board, nothing to link


def test_live_swig_altium_plane_loss_and_easyeda_disabled_layer_zones(kicad9, tmp_path):
    pcbdoc = sample("foreign", "altium", "eDP_adapter_dvt1.PcbDoc")
    res = fi.import_project([pcbdoc], tmp_path / "alt", board_backend="swig", installs=[kicad9])
    (step,) = res["steps"]
    assert step["step"] == "swig LoadBoard" and step["rc"] == 0, step["errors"]
    assert (step["statistics"]["footprints"], step["statistics"]["copper_layers"]) == (115, 4)
    assert step["result"]["inner_plane_loss"] == PLANE_LOSS
    assert res["status"] == "degraded" and res["human_steps"] == []     # explicit: degraded
    assert any("inner plane loss" in e and "P3.3V" in e for e in res["errors"])
    assert res["board_format_version"] == 20241229 and res["d0_dialect"] is True
    assert res["swig_readable"] is True and Path(res["project"]).is_file()
    watch = sample("foreign", "easyeda", "pcb_watch.json")
    res = fi.import_project([watch], tmp_path / "watch", board_backend="swig", installs=[kicad9])
    (step,) = res["steps"]
    assert res["status"] == "ok", (res["errors"], res["steps"])
    assert (step["statistics"]["footprints"], step["statistics"]["copper_layers"]) == (70, 2)
    assert any("(In1.Cu: 1, In2.Cu: 1)" in w for w in res["warnings"])
    assert res["source"]["tool"].startswith("pcbnew 9.") and "LoadBoard(EASYEDA)" in \
        res["source"]["tool"]


def test_live_auto_on_kicad9_alone_blocks_the_lossy_altium_import(kicad9, tmp_path):
    src = sample("foreign", "altium", "eDP_adapter_dvt1.PcbDoc")
    res = fi.import_project([src], tmp_path / "alt9", installs=[kicad9])
    (step,) = res["steps"]
    assert step["step"] == "swig LoadBoard" and step["status"] == "failed"
    assert res["status"] == "failed" and res["board"] is None
    assert [h["id"] for h in res["human_steps"]] == ["inner_plane_loss:eDP_adapter_dvt1.PcbDoc"]
    eagle = fi.import_project([sample("eagle", "Qwiic_Button.brd")], tmp_path / "eg9",
                              installs=[kicad9])
    assert eagle["status"] == "incomplete" and eagle["steps"] == []
    assert "returns None on KiCad 9" in eagle["human_steps"][0]["why"]


def test_live_library_conversion_d0_dialect(kicad9, installs, tmp_path):
    out = tmp_path / "libs"
    lbr = fi.convert_library(sample("libs", "SparkFun-IC-Memory.lbr"), out, installs=installs)
    assert lbr["status"] == "ok", lbr["errors"]
    fp, sym = lbr["libraries"]
    assert (fp["count"], fp["format_version"], fp["d0_dialect"]) == (12, 20241229, True)
    assert (sym["count"], sym["format_version"], sym["d0_dialect"]) == (22, 20241209, True)
    assert fp["tool"] == f"kicad-cli {kicad9.version}"
    pcblib = fi.convert_library(sample("libs", "ESP32-WROOM-32.PcbLib"), out, installs=installs)
    assert pcblib["status"] == "ok" and pcblib["libraries"][0]["count"] == 1
    assert pcblib["libraries"][0]["format_version"] == 20241229
    legacy = fi.convert_library(sample("libs", "Sensor_Distance.lib"), out, installs=installs)
    assert legacy["status"] == "ok" and legacy["libraries"][0]["format_version"] == 20241209


def test_live_easyeda_pro_library_needs_the_nightly(kicad9, nightly, tmp_path):
    res = fi.convert_library(sample("libs", "LS2K0300_Footprint.elibz2"), tmp_path / "libs",
                             installs=[kicad9, nightly])
    (fp,) = res["libraries"]
    assert fp["ok"] and fp["d0_dialect"] is False and fp["tool"] == f"kicad-cli {nightly.version}"
    assert fp["attempts"][0]["errors"] and res["status"] == "degraded"


def test_live_gui_only_easyeda_pro_board_and_altium_project(nightly, tmp_path):
    res = fi.import_project([sample("foreign", "eepro", "yuzuki.epro")], tmp_path / "yz",
                            installs=[nightly])
    assert res["status"] == "incomplete"
    assert len(res["schematics"]) == 5                       # root + 4 sub-sheets
    assert sum(s["symbols"] for s in res["statistics"]["schematics"].values()) == 333
    (step,) = res["human_steps"]
    assert step["role"] == "board" and "Non-KiCad Board File" in step["action"]
    proj = fi.import_project([sample("foreign", "altium", "eDP_adapter_dvt1.PrjPcb")],
                             tmp_path / "prj", installs=[nightly])
    (step,) = proj["human_steps"]
    assert "power.SchDoc" in step["documents"] and proj["steps"] == []
