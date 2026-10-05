"""foreign_formats: input classification, the per-install capability probe
and console scanning for foreign-format import (docs/ROADMAP_DECISIONS.md D4).

Unit tier (no KiCad): the probe runs REAL subprocesses of fake kicad-cli
flavours (tests/fixtures/fake_kicad.py) that answer `--help` like KiCad
9.0.4 (rc 1 on `fp upgrade --help`, "Failed to parse 'import'"), 10.0.5
(`pcb import` only; `sch import --help` prints the `sch` usage with rc 0)
and the 10.99 nightly; the cache and the not-cached transient failure are
checked by counting calls. Classification sniffs content where extensions
lie; scan_console keeps Error:/crash lines and warnings, drops chatter.

Live tier (skipped without KiCad): the real probe of every install.
"""
from __future__ import annotations

import sys
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

import foreign_formats as ff
import kicad_env as env
from fixtures.fake_kicad import (EAGLE_HEAD, altium, eagle_brd, eagle_sch, easyeda_pro,
                                 easyeda_std, fake_installer, log_cli_calls, prjpcb, read_calls,
                                 write)
from netlist_schema import SOURCE_KINDS


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
    """fake_install("k9" | "k10" | "k11") -> an install on that fake kicad-cli."""
    return fake_installer(tmp_path)


@pytest.fixture(scope="module")
def installs() -> list[env.KiCadInstall]:
    return env.discover()


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def test_classify_eagle_board_and_schematic(tmp_path):
    brd = ff.classify(eagle_brd(tmp_path))
    sch = ff.classify(eagle_sch(tmp_path))
    assert (brd.kind, brd.role, brd.pcb_format) == ("eagle", "board", "eagle")
    # LoadBoard(EAGLE) returns None on pcbnew 9 and works on 10 (D4 as amended)
    assert (brd.swig_plugin, brd.swig_min_major) == ("EAGLE", 10)
    assert (sch.kind, sch.role, sch.sch_format) == ("eagle", "schematic", "eagle")
    assert (sch.swig_plugin, sch.swig_min_major) == ("", 0)


def test_classify_refuses_a_non_eagle_brd(tmp_path):
    allegro = write(tmp_path / "board.brd", "binary allegro data")
    with pytest.raises(ff.ForeignFormatError, match=r"board\.brd: not an Eagle 6\+ XML"):
        ff.classify(allegro)


def test_classify_altium_and_easyeda(tmp_path):
    pcb = ff.classify(altium(tmp_path, "x.PcbDoc"))
    assert (pcb.kind, pcb.role, pcb.pcb_format, pcb.swig_plugin, pcb.swig_min_major) == \
        ("altium", "board", "altium", "ALTIUM_DESIGNER", 0)
    sch = ff.classify(altium(tmp_path, "power.SchDoc"))
    assert (sch.kind, sch.role, sch.sch_format) == ("altium", "schematic", "altium")
    board = ff.classify(easyeda_std(tmp_path, "pcb.json", "3"))
    assert (board.kind, board.role, board.pcb_format, board.swig_plugin) == \
        ("easyeda", "board", "auto", "EASYEDA")
    for doctype in ("1", "5"):
        s = ff.classify(easyeda_std(tmp_path, f"sch{doctype}.json", doctype))
        assert (s.kind, s.role, s.sch_format) == ("easyeda", "schematic", "easyeda")
    with pytest.raises(ff.ForeignFormatError, match=r"docType 4"):
        ff.classify(easyeda_std(tmp_path, "lib.json", "4"))


def test_classify_gui_only_cases(tmp_path):
    proj = ff.classify(prjpcb(tmp_path))
    assert proj.role == "project" and proj.gui_only
    assert proj.documents == ("power.SchDoc", "it6251_core.SchDoc", "eDP_adapter_dvt1.PcbDoc")
    pro = ff.classify(easyeda_pro(tmp_path))
    assert (pro.kind, pro.role, pro.sch_format) == ("easyeda", "schematic", "easyedapro")
    assert "bad_function_call" in pro.gui_only
    sheets_only = ff.classify(easyeda_pro(tmp_path, "s.epro", board=False))
    assert sheets_only.gui_only == ""
    board_only = ff.classify(easyeda_pro(tmp_path, "b.zip", sheets=False))
    assert board_only.role == "board" and board_only.gui_only
    empty = tmp_path / "other.zip"
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("readme.txt", "x")
    with pytest.raises(ff.ForeignFormatError, match="EasyEDA Pro"):
        ff.classify(empty)
    with pytest.raises(ff.ForeignFormatError, match=r"not an EasyEDA Pro project \("):
        ff.classify(write(tmp_path / "plain.epro", "not a zip archive"))


def test_classify_libraries(tmp_path):
    lbr = write(tmp_path / "mem.lbr", EAGLE_HEAD + "<library/></drawing></eagle>")
    assert (ff.classify(lbr).kind, ff.classify(lbr).role) == ("eagle", "library")
    assert ff.classify(lbr).library_kinds == ("footprint", "symbol")
    with pytest.raises(ff.ForeignFormatError, match="not an Eagle"):
        ff.classify(write(tmp_path / "old.lbr", "binary eagle 5 library"))
    assert ff.classify(altium(tmp_path, "esp.PcbLib")).library_kinds == ("footprint",)
    assert ff.classify(altium(tmp_path, "parts.IntLib")).library_kinds == ("footprint",)
    legacy = write(tmp_path / "Sensor.lib", "EESchema-LIBRARY Version 2.4\n#\n")
    assert (ff.classify(legacy).kind, ff.classify(legacy).library_kinds) == ("kicad", ("symbol",))
    ee = ff.classify(write(tmp_path / "x.elibz2", "PK"))
    assert (ee.kind, ee.role, ee.library_kinds) == ("easyeda", "library", ("footprint",))
    with pytest.raises(ff.ForeignFormatError, match="not a legacy KiCad"):
        ff.classify(write(tmp_path / "other.lib", "ar archive"))


def test_classify_unknown_and_missing(tmp_path):
    with pytest.raises(ff.ForeignFormatError, match=r"\.xyz.*supported"):
        ff.classify(write(tmp_path / "a.xyz", "?"))
    with pytest.raises(FileNotFoundError):
        ff.classify(tmp_path / "missing.brd")


def test_every_design_kind_is_a_d4_source_kind():
    assert set(ff.SOURCE_KINDS) <= set(SOURCE_KINDS)


# ---------------------------------------------------------------------------
# Console scanning
# ---------------------------------------------------------------------------
def test_scan_console_collects_errors_and_warnings():
    scan = ff.scan_console(
        "Importing 'power.SchDoc' using Altium format...\n"
        "Successfully saved imported schematic to 'x.kicad_sch'\n",
        "17:00:48: Error: No library symbol found for schematic symbol 'R10P 10k, 1%'.\n"
        "16:58:58: Setting schematic symbol 'R1 10k' library identifier to 'x:R'.\n"
        "5:00:48 PM: Font 'NotoSerifCJKsc-Medium' not found; substituting 'Verdana'.\n"
        "Imported footprint 'CAP 0603' has conflicting placed definitions; keeping the first.\n"
        "18:45:36: Altium layer Internal Plane 1 has no KiCad equivalent. It has been moved.\n"
        "Warning: something odd\n"
        "Fontconfig error: Cannot load default config file\n"
        "Unhandled exception class: std::bad_function_call\n"
        "No PCB importer recognizes the file format of 'outline.dxf'\n"
        "Unable to convert library\n")
    assert scan.errors == ("No library symbol found for schematic symbol 'R10P 10k, 1%'.",
                           "Unhandled exception class: std::bad_function_call",
                           "No PCB importer recognizes the file format of 'outline.dxf'",
                           "Unable to convert library")
    assert scan.warnings == (
        "Font 'NotoSerifCJKsc-Medium' not found; substituting 'Verdana'.",
        "Imported footprint 'CAP 0603' has conflicting placed definitions; keeping the first.",
        "Altium layer Internal Plane 1 has no KiCad equivalent. It has been moved.",
        "something odd")


# ---------------------------------------------------------------------------
# Capability probe (real subprocesses of fake CLIs)
# ---------------------------------------------------------------------------
def test_help_supports_reads_the_usage_line_not_the_exit_code():
    assert ff.help_supports("input: 1 argument(s) expected. 0 provided.\n"
                            "Usage: upgrade [--help] [--output OUTPUT_DIR] INPUT_DIR",
                            ("fp", "upgrade"))
    assert not ff.help_supports("Failed to parse 'import', did you mean 'export'\n"
                                "Usage: pcb [--help] {drc,export,render}", ("pcb", "import"))
    assert not ff.help_supports("Usage: kicad-cli sch [--help] {erc,export,upgrade}",
                                ("sch", "import"))
    assert ff.help_supports("Usage: kicad-cli import [--help] [--output PROJECT] INPUT_FILES...",
                            ("import",))
    assert not ff.help_supports("Usage: sym upgrade [--help] INPUT", ("fp", "upgrade"))
    assert not ff.help_supports("", ("import",))


def test_probe_kicad9_fake(fake_install):
    caps = ff.capabilities(fake_install("k9"))
    assert not (caps.project_import or caps.pcb_import or caps.sch_import)
    assert caps.fp_upgrade and caps.sym_upgrade          # despite rc=1 on --help
    assert caps.pcb_formats == () and caps.probe_errors == ()


def test_probe_kicad10_fake(fake_install):
    caps = ff.capabilities(fake_install("k10"))
    assert caps.pcb_import and caps.pcb_report_json and not caps.layer_map
    assert not caps.sch_import and not caps.project_import
    assert "eagle" in caps.pcb_formats and "altium" in caps.pcb_formats


def test_probe_nightly_fake(fake_install):
    caps = ff.capabilities(fake_install("k11"))
    assert caps.project_import and caps.pcb_import and caps.sch_import
    assert caps.pcb_report_json and caps.sch_report_json and caps.layer_map
    assert "easyedapro" in caps.sch_formats and caps.fp_upgrade and caps.sym_upgrade
    assert caps.tool == "kicad-cli 10.99.0-4025-gfake"
    assert caps.as_dict()["kicad_cli"] == str(caps.kicad_cli)


def test_probe_is_cached_per_install(fake_install, cli_log):
    inst = fake_install("k11")
    first = ff.capabilities(inst)
    n = len(read_calls(cli_log))
    assert n == 5                                        # one --help per command
    assert ff.capabilities(inst) == first and len(read_calls(cli_log)) == n
    ff.clear_cache()
    ff.capabilities(inst)
    assert len(read_calls(cli_log)) == 2 * n


def test_cache_holds_only_cli_fields_python_and_version_come_per_call(fake_install, cli_log,
                                                                      tmp_path):
    """A transient `import pcbnew` probe left the first discover() without a
    python; the next discover() finds it: the cached CLI probe must not pin
    python=None (nor an old version) for the life of the process."""
    without = fake_install("k9")
    assert ff.capabilities(without).python is None
    n = len(read_calls(cli_log))
    python = tmp_path / "python.exe"
    found = replace(without, python=python, version="9.0.4-1", version_tuple=(9, 0, 5))
    caps = ff.capabilities(found)
    assert len(read_calls(cli_log)) == n                 # the CLI was not probed again
    assert caps.python == python and caps.as_dict()["python"] == str(python)
    assert (caps.version, caps.version_tuple) == ("9.0.4-1", (9, 0, 5))
    assert caps.fp_upgrade and not caps.pcb_import       # the cached CLI-derived fields
    assert ff.capabilities(without).python is None       # and back, per call


def test_transient_probe_failure_is_not_cached(fake_install, monkeypatch):
    inst = fake_install("k11")
    real_run = ff.run_child
    timeouts = {"left": 1}

    def flaky(argv, timeout_s, cwd=None):
        if timeouts["left"]:
            timeouts["left"] -= 1
            return env.ProbeResult(env.TRANSIENT_RC, "", f"{argv[0]}: timed out after 30 s")
        return real_run(argv, timeout_s, cwd)

    monkeypatch.setattr(ff, "run_child", flaky)
    caps = ff.capabilities(inst)
    assert not caps.project_import and caps.probe_errors
    assert "timed out" in caps.probe_errors[0]
    again = ff.capabilities(inst)                        # probed again, now settled
    assert again.project_import and again.probe_errors == ()
    assert ff.capabilities(inst) == again


def test_discover_capabilities_newest_first(fake_install):
    caps = ff.discover_capabilities([fake_install("k9"), fake_install("k11"),
                                     fake_install("k10")])
    assert [c.version_tuple[:2] for c in caps] == [(10, 99), (10, 0), (9, 0)]


# ---------------------------------------------------------------------------
# run_child: stdin detached, timeouts and launch failures are TRANSIENT_RC
# ---------------------------------------------------------------------------
def test_run_child_detaches_stdin_and_decodes_utf8(tmp_path):
    r = ff.run_child([sys.executable, "-c", "import sys; d = sys.stdin.read(); "
                      "sys.stdout.buffer.write(('in=%d \\u00b5' % len(d)).encode('utf-8'))"],
                     30, cwd=tmp_path)
    assert r.returncode == 0 and r.stdout == "in=0 µ"


def test_run_child_timeout_and_launch_failure(tmp_path):
    slow = ff.run_child([sys.executable, "-c", "import time; print('x', flush=True); "
                         "time.sleep(30)"], 1)
    assert slow.returncode == env.TRANSIENT_RC and "timed out after 1 s" in slow.stderr
    missing = ff.run_child([str(tmp_path / "no-such-kicad-cli.exe"), "--help"], 5)
    assert missing.returncode == env.TRANSIENT_RC and "cannot start" in missing.stderr
    assert missing.stdout == ""


def test_decode_accepts_bytes_text_and_none():
    assert ff._decode("µ".encode("utf-8")) == "µ"
    assert ff._decode(b"\xff") == "�"
    assert ff._decode("text") == "text" and ff._decode(None) == ""



# ---------------------------------------------------------------------------
# Live tier
# ---------------------------------------------------------------------------
def test_live_capability_probe(installs):
    if not installs:
        pytest.skip("no KiCad installed")
    for caps in ff.discover_capabilities(installs):
        assert caps.fp_upgrade and caps.sym_upgrade, caps
        if caps.version_tuple[0] == 9:
            assert not (caps.pcb_import or caps.sch_import or caps.project_import), caps
        if caps.version_tuple >= (10, 99, 0):
            assert caps.pcb_import and caps.sch_import and caps.project_import, caps
            assert caps.pcb_report_json and caps.sch_report_json and caps.layer_map, caps
            assert "eagle" in caps.pcb_formats and "easyedapro" in caps.sch_formats
