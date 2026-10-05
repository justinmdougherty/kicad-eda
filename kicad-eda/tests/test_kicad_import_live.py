"""Live tier of the KiCad project importer (tests/test_kicad_import.py is the
unit tier): real kicad-cli exports and real pcbnew footprint extraction.

Every test skips cleanly when what it needs is missing; nothing probes KiCad
at import or collection time (the installs are looked up in module fixtures).

* The committed fixture, linked the way KiCad links a board to its
  schematic, converts with its own footprints, extracted by the pcbnew
  convert() picks (the oldest >= 9.0: KiCad 9 locally, KiCad 10 in the
  Docker/CI image); the library is in that pcbnew's dialect, which the report
  records (D0 dialect = KiCad 9's 20241229), and keeps no instance data.
* KiCad 9 demos (taken from a KiCad 9.x install only) convert and pass
  netlist_schema, lint and net_equivalence against their source boards.
* One footprint placed on many sheets is one library footprint, whichever
  pcbnew extracts. Its KiCad 9 demo comes from a 9.x install or from
  ``$KICAD9_DEMOS`` (set it in the KiCad 10 image to a mounted KiCad 9
  share/kicad/demos, so pcbnew 10 runs it).
* A large board (vme-wren, 1508 footprints) extracts in seconds.
* A board newer than KiCad 9's format (the newest install's demo) records
  that the KiCad 11 path is required.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import kicad_import as ki
import kicad_import_checks as kchk
import kicad_import_fplib as kf
import kicad_import_pcb as kp
import net_equivalence as ne

FIX = Path(__file__).resolve().parent / "fixtures" / "kicad" / "kimport"
PCB = FIX / "kimport.kicad_pcb"
LARGE_BOARD_LIMIT_S = 60.0


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _comp(design: dict, ref: str) -> dict:
    return next(c for c in design["components"] if c["ref"] == ref)


# ----------------------------------------------------------------------------
# Installs, looked up lazily
# ----------------------------------------------------------------------------
@pytest.fixture(scope="module")
def installs() -> list:
    import kicad_env as env
    found = env.discover()
    if not found:
        pytest.skip("no kicad-cli installed")
    return found


@pytest.fixture(scope="module")
def swig(installs: list):
    """The install whose pcbnew convert() extracts a KiCad 9 board with."""
    install = kf.choose_swig_install(kf.K9_PCB_FORMAT, installs)
    if install is None:
        pytest.skip("no KiCad >= 9.0 whose bundled Python imports pcbnew")
    return install


@pytest.fixture(scope="module")
def kicad9(installs: list):
    """A KiCad 9.x install with pcbnew: its demos are KiCad 9 format."""
    install = next((i for i in installs if i.version_tuple[0] == 9 and i.share), None)
    if install is None or install.python is None:
        pytest.skip("no KiCad 9.x install with pcbnew (its demos are the KiCad 9 format)")
    return install


def _demo(install, demo: str, name: str) -> Path:
    path = install.share / "demos" / demo / name
    if not path.is_file():
        pytest.skip(f"demo {demo} not installed under {install.share}")
    return path


@pytest.fixture(scope="module")
def kicad9_demos(installs: list) -> Path:
    """KiCad 9 format demo projects: ``$KICAD9_DEMOS`` (e.g. a KiCad 9
    share/kicad/demos mounted into the KiCad 10 Docker image), else those of
    a KiCad 9.x install."""
    env_dir = os.environ.get("KICAD9_DEMOS")
    if env_dir:
        if not Path(env_dir).is_dir():
            pytest.fail(f"KICAD9_DEMOS={env_dir} is not a directory")
        return Path(env_dir)
    install = next((i for i in installs if i.version_tuple[0] == 9 and i.share), None)
    if install is None or not (install.share / "demos").is_dir():
        pytest.skip("no KiCad 9 demos (no KiCad 9.x install; KICAD9_DEMOS unset)")
    return install.share / "demos"


_INSTANCE_ITEM = re.compile(r'^\t\((sheetname|sheetfile|path)\b|^\t\(property\s+("?)([^"\s()]+)\2',
                            re.M)


def instance_data(text: str) -> list[str]:
    """What a library footprint saved by pcbnew still carries of the board
    instance it came from: top-level path / sheetname / sheetfile items and
    properties outside KEEP_FIELDS (pcbnew writes top-level items one tab in)."""
    return [m.group(1) or m.group(3) for m in _INSTANCE_ITEM.finditer(text)
            if m.group(1) or m.group(3) not in kf.KEEP_FIELDS]


_COMP_XML = re.compile(r'<comp ref="([^"]+)">.*?</comp>', re.S)


def link_like_kicad(board_text: str, xml_text: str, sheets: dict | None = None) -> str:
    """The board as KiCad's "Update PCB from schematic" links it: each
    schematic part's footprint gets the symbol's footprint filters (a bare
    ``ki_fp_filters`` property), its symbol path, sheet name and sheet file.
    ``sheets`` {ref: (sheetname, sheetfile)} moves parts onto other sheets."""
    for m in _COMP_XML.finditer(xml_text):
        ref, body = m.group(1), m.group(0)
        stamp = re.search(r"<tstamps>([^<]+)</tstamps>", body).group(1)
        filters = re.search(r'name="ki_fp_filters" value="([^"]*)"', body)
        sheet, sheet_file = (sheets or {}).get(ref, ("/", "kimport.kicad_sch"))
        anchor = f'(property "Reference" "{ref}"'
        linked = ((f'(property ki_fp_filters "{filters.group(1)}")\n\t\t' if filters else "")
                  + f'(path "{sheet}{stamp}")\n\t\t(sheetname "{sheet}")\n\t\t'
                  f'(sheetfile "{sheet_file}")\n\t\t')
        board_text = board_text.replace(anchor, linked + anchor, 1)
    return board_text


def _linked_fixture(tmp_path: Path) -> Path:
    """A copy of the fixture project whose board KiCad has linked to the
    schematic (link_like_kicad)."""
    project = tmp_path / "kimport"
    project.mkdir()
    for name in ("kimport.kicad_pro", "kimport.kicad_sch"):
        shutil.copyfile(FIX / name, project / name)
    linked = link_like_kicad(_text(PCB), _text(FIX / "kimport.xml"))
    assert linked.count("(sheetfile ") == 7 and linked.count("(property ki_fp_filters") == 7
    (project / "kimport.kicad_pcb").write_text(linked, encoding="utf-8", newline="\n")
    return project


def _check(res: dict) -> None:
    checks = res["report"]["checks"]
    assert checks["schema_validated"] and checks["schema_errors"] == [], \
        checks.get("schema_note") or checks["schema_errors"][:10]
    assert checks["lint"]["ok"], checks["lint"]["errors"][:10]
    assert checks["netlist_vs_pcb"]["equal"], checks["netlist_vs_pcb"]["summary"]
    assert checks["library_paths"]["too_long"] == []
    assert res["ok"]


_LOAD_SCRIPT = """
import wx; wx.DisableAsserts(); wx.Log.SetActiveTarget(wx.LogStderr())
import json, os, sys
import pcbnew
lib = sys.argv[1]
out = {}
for f in sorted(os.listdir(lib)):
    fp = pcbnew.FootprintLoad(lib, f[:-len('.kicad_mod')])
    out[f] = None if fp is None else sorted(p.GetNumber() for p in fp.Pads())
print(json.dumps(out))
"""


def _load_with_pcbnew(python: Path, pretty: Path, tmp_path: Path) -> dict:
    script = tmp_path / "load_fps.py"
    script.write_text(_LOAD_SCRIPT, encoding="utf-8")
    r = subprocess.run([str(python), str(script), str(pretty)], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=300)
    assert r.returncode == 0, r.stderr[-1500:]
    return json.loads(r.stdout.strip().splitlines()[-1])


# ----------------------------------------------------------------------------
# The fixture
# ----------------------------------------------------------------------------
def test_live_fixture_converts_with_its_own_footprints(tmp_path: Path, installs: list,
                                                       swig) -> None:
    """The fixture as KiCad links it (symbol paths, sheets, footprint
    filters on every part): the library keeps none of that, whichever
    pcbnew saved it (KiCad 10 writes sheetname / sheetfile / ki_fp_filters
    into a library copy, KiCad 9 does not)."""
    res = ki.convert(_linked_fixture(tmp_path), tmp_path / "out")
    _check(res)
    design, report = res["design"], res["report"]
    oldest = min((i for i in installs if i.version_tuple >= (9, 0, 0)),
                 key=lambda i: i.version_tuple)
    assert report["source"]["tool"] == f"kicad-cli {oldest.version}"
    info = report["libraries"]["footprints"]
    assert info["extracted"] and info["python"] == str(swig.python)
    assert info["pcbnew"].startswith(f"{swig.version_tuple[0]}.")
    # C1 (B side, 45 degrees) and C2 (F side) share one library footprint
    assert _comp(design, "C1")["footprint"] == _comp(design, "C2")["footprint"] == \
        "kimport_imported:C_0603_1608Metric"
    pretty = tmp_path / "out" / "libs" / "kimport_imported.pretty"
    text = _text(pretty / "C_0603_1608Metric.kicad_mod")
    assert f"(version {info['format']})" in text and '"REF**"' in text and "(net " not in text
    assert "dnp" not in text                     # C2's DNP flag stays on the instance
    assert info["d0_dialect"] is (info["format"] == kf.K9_PCB_FORMAT)
    if swig.version_tuple[0] == 9:               # D0: KiCad 9 writes 20241229
        assert info["format"] == kf.K9_PCB_FORMAT
    else:                                        # KiCad 10 only (Docker): reported
        assert any("KiCad 9 cannot load" in w for w in info["warnings"])
    saved = sorted(pretty.glob("*.kicad_mod"))
    assert len(saved) == info["footprints"] and info["variants"] == []
    assert {p.name: instance_data(_text(p)) for p in saved} == {p.name: [] for p in saved}
    loaded = _load_with_pcbnew(swig.python, pretty, tmp_path)
    board = {fp.ref: fp for fp in kp.read_board(_text(PCB)).footprints}
    for comp in design["components"]:
        name = comp["footprint"].split(":", 1)[1] + ".kicad_mod"
        assert loaded[name] is not None, f"pcbnew {swig.version} cannot load {name}"
        assert [n for n in loaded[name] if n] == sorted(p.number for p in board[comp["ref"]].pads)


_EXTRA_FIELDS = ('(property "MPN" "RC0603FR-071KL" (at 0 0 0) (layer "F.Fab") (hide yes) '
                 '(uuid "6f1c1d6e-1111-4222-8333-944455566601") (effects (font (size 1 1) '
                 '(thickness 0.15))))\n\t\t(property "LCSC" "C21190" (at 0 0 0) (layer "F.Fab") '
                 '(hide yes) (uuid "6f1c1d6e-1111-4222-8333-944455566602") (effects (font '
                 '(size 1 1) (thickness 0.15))))\n\t\t')


def test_live_instance_fields_are_dropped_from_library_copies(tmp_path: Path, swig) -> None:
    """Parts on different sheets, two of them carrying schematic fields (MPN,
    LCSC): pcbnew keeps every field in its library copy (kicad_import_swig
    removes none, pcbnew 10 cannot do it safely) and pcbnew 10 the sheet too;
    kicad_import_fplib.strip_instance_fields drops them from the text, so
    each pair of one footprint is still one library footprint."""
    sheets = {"R1": ("/amp_a/", "amp.kicad_sch"), "C1": ("/supply/", "supply.kicad_sch")}
    text = link_like_kicad(_text(PCB), _text(FIX / "kimport.xml"), sheets)
    for ref in ("R1", "C1"):                          # C1: the B-side, 45 degree part
        anchor = f'(property "Reference" "{ref}"'
        text = text.replace(anchor, _EXTRA_FIELDS + anchor, 1)
    pcb = tmp_path / "fields.kicad_pcb"
    pcb.write_text(text, encoding="utf-8")
    board = kp.read_board(text)
    lib = kf.extract_footprint_library(pcb, board, board.footprints, "f", tmp_path / "f.pretty",
                                       str(swig.python))
    name = {fp.ref: lib.names[fp.index] for fp in board.footprints}
    assert name["R1"] == name["R2"] and name["C1"] == name["C2"] and lib.variants == ()
    for path in map(Path, lib.files):
        saved = _text(path)
        assert instance_data(saved) == [], path.name
        assert '"Reference"' in saved and '"Datasheet"' in saved


def test_live_extracted_libraries_parse_with_kicad_cli(tmp_path: Path, swig) -> None:
    ki.convert(FIX, tmp_path / "out")
    libs = tmp_path / "out" / "libs"
    for args in (["fp", "upgrade", "--force", "-o", str(tmp_path / "up.pretty"),
                  str(libs / "kimport_imported.pretty")],
                 ["sym", "upgrade", "--force", "-o", str(tmp_path / "up.kicad_sym"),
                  str(libs / "kimport_imported.kicad_sym")]):
        r = subprocess.run([str(swig.kicad_cli), *args], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=300)
        assert r.returncode == 0 and "Error" not in r.stdout + r.stderr, r.stdout + r.stderr


# ----------------------------------------------------------------------------
# KiCad 9 demos
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("demo, project, footprints", [
    ("stickhub", "StickHub.kicad_pro", True),              # B side at 45 deg, project libraries
    ("complex_hierarchy", "complex_hierarchy.kicad_pro", True),   # one sheet used twice
    ("multichannel", "multichannel_mixer.kicad_pro", False),      # one sheet used four times
    ("tiny_tapeout", "tinytapeout-demo.kicad_pro", True),         # board-only STITCH1 x 130
])
def test_live_kicad9_demos_convert_and_round_trip(tmp_path: Path, kicad9, demo: str,
                                                  project: str, footprints: bool) -> None:
    src = _demo(kicad9, demo, project)
    board_text = _text(src.with_suffix(".kicad_pcb"))
    assert (ne.pcb_format_version(board_text) or 0) <= kf.K9_PCB_FORMAT
    before = sorted(p.name for p in src.parent.iterdir())
    res = ki.convert(src, tmp_path / "out", extract_footprints=footprints)
    _check(res)
    assert sorted(p.name for p in src.parent.iterdir()) == before     # source untouched
    design, report = res["design"], res["report"]
    assert report["sch_vs_pcb"]["equal"], report["sch_vs_pcb"]["summary"]
    # strict, renamed parts included (tiny_tapeout renames 136 footprints: 129 of its 130
    # STITCH1 vias, two STITCH6, three G*** logos, two REF**)
    named = kchk.board_as_imported(board_text, report["renamed_refs"])
    assert ne.compare(ne.netlist_pad_nets(design), ne.pcb_pad_nets(named)).equal
    checked = report["checks"]["netlist_vs_pcb"]
    assert checked["details"]["ignored_refs"] == []
    assert len(checked["renamed_parts"]) == len(report["renamed_refs"])
    info = report["libraries"]["footprints"]
    assert info["extracted"] is footprints
    if footprints:                               # KiCad 9 present: picked, D0 dialect
        assert info["pcbnew"].startswith("9.") and info["d0_dialect"]
    assert all(c["footprint"].startswith(f"{design['name']}_imported:") == footprints
               for c in design["components"])


def test_live_stickhub_b_side_45_degree_part_is_unflipped_exactly(tmp_path: Path,
                                                                 kicad9) -> None:
    """C36 sits on B.Cu at 45 degrees; its extracted footprint has the pads
    of the project's own 1608_C (pad angles aside: KiCad stores the 0603
    pads at 180 in the library, the symmetric shape is the same)."""
    src = _demo(kicad9, "stickhub", "StickHub.kicad_pro")
    res = ki.convert(src, tmp_path / "out")
    c36 = _comp(res["design"], "C36")
    assert c36["side"] == "B" and c36["at"][2] == 45.0
    name = c36["footprint"].split(":", 1)[1]
    got = tmp_path / "out" / "libs" / "StickHub_imported.pretty" / f"{name}.kicad_mod"
    pad_re = re.compile(r'\(pad "(\d+)" smd roundrect\s+\(at ([-\d.]+) ([-\d.]+)')
    pads = pad_re.findall(_text(got))
    assert len(pads) == 2
    assert pads == pad_re.findall(_text(src.parent / "footprints.pretty" / "1608_C.kicad_mod"))


def test_live_one_footprint_on_many_sheets_is_one_library_footprint(tmp_path: Path, swig,
                                                                    kicad9_demos: Path) -> None:
    """complex_hierarchy places its 10 footprints 68 times over five sheets:
    one library footprint per board footprint and no variants, whichever
    pcbnew extracts (pcbnew 10 used to give 20, each sheet a variant; pcbnew
    9 gave 11, its texts written in another order)."""
    pcb = kicad9_demos / "complex_hierarchy" / "complex_hierarchy.kicad_pcb"
    if not pcb.is_file():
        pytest.skip(f"{pcb} not found")
    board = kp.read_board(_text(pcb))
    lib = kf.extract_footprint_library(pcb, board, board.footprints, "ch", tmp_path / "ch.pretty",
                                       str(swig.python))
    ids = {fp.lib_id for fp in board.footprints}
    assert (len(board.footprints), len(ids)) == (68, 10)
    assert len(lib.files) == len(ids) and lib.variants == (), [v["name"] for v in lib.variants]
    assert {Path(f).name: instance_data(_text(Path(f))) for f in lib.files} == {
        Path(f).name: [] for f in lib.files}
    assert lib.pcbnew.startswith(f"{swig.version_tuple[0]}.")


def test_live_large_board_extracts_in_seconds(tmp_path: Path, kicad9) -> None:
    """vme-wren's 1508 footprints: one library directory per save keeps
    pcbnew from re-reading a growing library (it took 9 minutes before)."""
    pcb = _demo(kicad9, "vme-wren", "vme-wren.kicad_pcb")
    board = kp.read_board(_text(pcb))
    start = time.monotonic()
    lib = kf.extract_footprint_library(pcb, board, board.footprints, "w",
                                       tmp_path / "w.pretty", str(kicad9.python))
    elapsed = time.monotonic() - start
    assert elapsed < LARGE_BOARD_LIMIT_S, f"{elapsed:.0f} s for {len(board.footprints)} footprints"
    assert set(lib.names) == {fp.index for fp in board.footprints}
    assert lib.format == kf.K9_PCB_FORMAT and len(lib.files) == len(set(lib.names.values()))


# ----------------------------------------------------------------------------
# A board newer than KiCad 9's format
# ----------------------------------------------------------------------------
def test_live_newer_format_board_records_the_kicad11_path(tmp_path: Path, installs: list) -> None:
    newest = max(installs, key=lambda i: i.version_tuple)
    if newest.share is None:
        pytest.skip(f"KiCad {newest.version} ships no share/kicad")
    pcb = _demo(newest, "pic_programmer", "pic_programmer.kicad_pcb")
    if (ne.pcb_format_version(_text(pcb)) or 0) <= kf.K9_PCB_FORMAT:
        pytest.skip(f"KiCad {newest.version}'s pic_programmer board is KiCad 9 format")
    res = ki.convert(pcb.with_suffix(".kicad_pro"), tmp_path / "out")
    _check(res)
    info = res["report"]["libraries"]["footprints"]
    assert info["extracted"] is False and info["kicad11_path_required"] is True
    assert res["report"]["source"]["tool"] == f"kicad-cli {newest.version}"
