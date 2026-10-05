"""foreign_import + swig_load_foreign: foreign designs through KiCad's own
importers (docs/ROADMAP_DECISIONS.md D4, F6).

Unit tier (no KiCad): every KiCad child is a REAL subprocess of a fake
kicad-cli (tests/fixtures/fake_kicad.py) that imports / converts by writing
small KiCad-shaped files, JSON reports and console lines -- including rc=0
runs that print "Error:" lines or write nothing. The SWIG fallback runs
swig_load_foreign.py under the venv Python against a stub pcbnew. Covers the
board/schematic/project flow and its report, console Error: capture, the
SWIG fallback and its Eagle refusal, library conversion on the OLDEST capable
install (D0 dialect), and the GUI-only human_step entries.

Live tier (skipped cleanly without KiCad / the nightly / the sample files):
the Eagle Qwiic_Button board+schematic import on the nightly; an Altium
SchDoc whose importer prints Error: lines with rc=0 and an empty report; the
KiCad 9 SWIG fallback for Altium and EasyEDA boards (no dialogs); library
conversion into the D0 dialect; the EasyEDA Pro / .PrjPcb human steps.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import foreign_formats as ff
import foreign_import as fi
import kicad_env as env
from fixtures.fake_kicad import (EAGLE_HEAD, altium, eagle_brd, eagle_sch, easyeda_pro,
                                 easyeda_std, fake_installer, kicad9_install, log_cli_calls,
                                 nightly_install, prjpcb, read_calls, sample, sha256,
                                 stub_pcbnew_python, work_calls, write)

ROOT = Path(__file__).resolve().parents[1]
SWIG_SCRIPT = ROOT / "server" / "tools" / "swig_load_foreign.py"


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
                             "tool": "kicad-cli 10.99.0-4025-gfake"}
    assert [s["role"] for s in res["sources"]] == ["board", "schematic"]
    assert res["sources"][1]["sha256"] == sha256(sch)
    assert res["board_format_version"] == 20260901 and res["d0_dialect"] is False
    assert any("20260901" in w and "D0" in w for w in res["warnings"])
    assert res["footprint_links"] == {"board": ["Qwiic_Button-import-fps"],
                                      "schematic": ["qb"], "consistent": False}
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
    assert saved["board"] == res["board"] and saved["status"] == "ok"
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
    with pytest.raises(ValueError, match="never Eagle"):
        fi.import_project([eagle_brd(src)], tmp_path / "o", board_backend="swig", installs=k11)
    with pytest.raises(ValueError, match="board_backend"):
        fi.import_project([eagle_brd(src)], tmp_path / "o", board_backend="gui", installs=k11)


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
    res = fi.import_project([eagle_brd(src), eagle_sch(src)], tmp_path / "out",
                            installs=[fake_install("k10")])
    assert res["status"] == "incomplete" and res["board"] and res["schematics"] == []
    (step,) = res["human_steps"]
    assert step["role"] == "schematic" and "sch import" in step["why"]
    assert "File > Import > Non-KiCad Schematic" in step["action"]
    assert res["board_format_version"] == 20260206


# ---------------------------------------------------------------------------
# SWIG fallback (swig_load_foreign.py under a stub pcbnew)
# ---------------------------------------------------------------------------
def _run_swig(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SWIG_SCRIPT), *args], capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, timeout=60)


def test_swig_script_loads_and_saves_an_altium_board(tmp_path, stub_python):
    _, calls = stub_python
    src = altium(tmp_path, "x.PcbDoc")
    out = tmp_path / "x.kicad_pcb"
    proc = _run_swig([str(src), str(out), "ALTIUM_DESIGNER"])
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["ok"] and result["footprints"] == 5 and result["tracks"] == 4
    assert result["vias"] == 2 and result["copper_layers"] == 2 and result["pcbnew"] == "9.0.4"
    assert out.is_file()
    assert "LoadBoard x.PcbDoc 5 quiet_first=True" in calls.read_text(encoding="utf-8")


def test_swig_script_refuses_eagle_before_loading(tmp_path, stub_python):
    _, calls = stub_python
    proc = _run_swig([str(eagle_brd(tmp_path)), str(tmp_path / "e.kicad_pcb"), "EAGLE"])
    assert proc.returncode == 2
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert not result["ok"] and "EAGLE refused" in result["error"]
    assert not calls.exists() or "LoadBoard" not in calls.read_text(encoding="utf-8")


def test_swig_script_reports_load_failures(tmp_path, stub_python):
    none = altium(tmp_path, "none.PcbDoc", "FAKE_NONE")
    proc = _run_swig([str(none), str(tmp_path / "n.kicad_pcb"), "ALTIUM_DESIGNER"])
    assert proc.returncode == 1 and "returned None" in proc.stdout
    proc = _run_swig([str(tmp_path / "missing.PcbDoc"), str(tmp_path / "m.kicad_pcb"),
                      "EASYEDA"])
    assert proc.returncode == 1 and "input not found" in proc.stdout
    proc = _run_swig([str(none), str(tmp_path / "n.kicad_pcb"), "PADS"])
    assert proc.returncode == 2 and "not one of" in proc.stdout
    proc = _run_swig([str(none)])
    assert proc.returncode == 2 and "usage" in proc.stdout


def test_auto_falls_back_to_swig_without_a_board_cli(tmp_path, fake_install, stub_python):
    python, calls = stub_python
    out = tmp_path / "out"
    res = fi.import_project([altium(tmp_path / "src", "eDP.PcbDoc")], out,
                            installs=[fake_install("k9", python=python)])
    assert res["status"] == "ok", res["errors"]
    (step,) = res["steps"]
    assert step["step"] == "swig LoadBoard" and step["statistics"]["footprints"] == 5
    assert step["tool"] == "pcbnew 9.0.4 (SWIG LoadBoard ALTIUM_DESIGNER)"
    assert res["board"] == str(out / "eDP.kicad_pcb") and res["d0_dialect"] is True
    assert res["source"]["tool"] == step["tool"]
    assert any("no KiCad equivalent" in w for w in res["warnings"])
    assert "quiet_first=True" in calls.read_text(encoding="utf-8")


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


def test_eagle_never_goes_through_swig(tmp_path, fake_install, stub_python):
    python, calls = stub_python
    res = fi.import_project([eagle_brd(tmp_path / "src")], tmp_path / "out",
                            installs=[fake_install("k9", python=python)])
    assert res["status"] == "incomplete" and res["steps"] == []
    (step,) = res["human_steps"]
    assert step["role"] == "board" and "Eagle" in step["why"]
    assert not calls.exists()


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
    assert fp["ok"] and fp["tool"] == "kicad-cli 10.99.0-4025-gfake"
    assert fp["format_version"] == 20260901 and fp["d0_dialect"] is False
    assert [a["tool"] for a in fp["attempts"]] == ["kicad-cli 9.0.4",      # oldest first
                                                   "kicad-cli 10.99.0-4025-gfake"]
    assert fp["attempts"][0]["errors"] == ["Unable to convert library"]
    assert res["status"] == "degraded" and any("D0" in w for w in res["warnings"])
    assert fp["path"] == str(tmp_path / "libs" / "LS2K.pretty") and fp["count"] == 2


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
    assert res["source"]["tool"] == f"kicad-cli {nightly.version}"
    assert res["footprint_links"]["consistent"] is False      # the importer's nickname quirk
    for step in res["steps"]:                                  # every console line was scanned
        log = Path(step["log"]).read_text(encoding="utf-8")
        assert step["console_errors"] == list(ff.scan_console(log).errors)
    assert res["errors"] == []


def test_live_altium_schdoc_errors_are_captured_despite_rc0(nightly, tmp_path):
    sch = sample("foreign", "altium", "power.SchDoc")
    res = fi.import_project([sch], tmp_path / "alt", installs=[nightly])
    (step,) = res["steps"]
    assert step["rc"] == 0 and step["report"]["errors"] == []
    assert len(step["console_errors"]) >= 40 and step["status"] == "degraded"
    assert any("No library symbol found" in e for e in res["errors"])
    assert res["status"] == "degraded"


def test_live_swig_fallback_altium_and_easyeda(kicad9, tmp_path):
    cases = [(sample("foreign", "altium", "eDP_adapter_dvt1.PcbDoc"), 115, 4),
             (sample("foreign", "easyeda", "pcb_watch.json"), 70, 2)]
    for src, footprints, copper in cases:
        out = tmp_path / src.stem
        res = fi.import_project([src], out, board_backend="swig", installs=[kicad9])
        assert res["status"] == "ok", (res["errors"], res["steps"])
        (step,) = res["steps"]
        assert step["step"] == "swig LoadBoard" and step["rc"] == 0
        assert step["statistics"]["footprints"] == footprints
        assert step["statistics"]["copper_layers"] == copper
        assert res["board_format_version"] == 20241229 and res["d0_dialect"] is True
        assert res["swig_readable"] is True and Path(res["project"]).is_file()


def test_live_auto_uses_swig_when_only_kicad9_is_given(kicad9, tmp_path):
    src = sample("foreign", "altium", "eDP_adapter_dvt1.PcbDoc")
    res = fi.import_project([src], tmp_path / "alt9", installs=[kicad9])
    assert res["status"] == "ok" and res["steps"][0]["step"] == "swig LoadBoard"
    eagle = fi.import_project([sample("eagle", "Qwiic_Button.brd")], tmp_path / "eg9",
                              installs=[kicad9])
    assert eagle["status"] == "incomplete" and eagle["steps"] == []


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
