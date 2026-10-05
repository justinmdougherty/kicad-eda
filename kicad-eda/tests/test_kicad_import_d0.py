"""server/tools/kicad_import_d0.py: the D0 footprint routes of the KiCad
project importer (ROADMAP D0/D4), unit tier (no KiCad).

provide_footprints tries, in order, 'pcbnew9' (a KiCad 9 board: today's
extraction), 'pcbnew10+downgrade' (a KiCad 10 board extracted by pcbnew >= 10
and downgraded with kicad_dialect), 'importer_library' (the KiCad 11
importer's <stem>-import-fps.pretty, downgraded, pads verified against the
board) and 'project_tables' (the source project's fp-lib-table libraries);
else the report says the KiCad 11 path is required, with every route's
reason. pcbnew is a stand-in script (one library directory per footprint,
like kicad_import_swig.py) and the KiCad 9 load check is stubbed; the real
ones run in tests/test_kicad_import_live.py. Also here: 3D model paths,
the symbol-library downgrade of kicad_import_sch and convert()'s
footprint_sources pass-through.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import types
from pathlib import Path

import pytest

import kicad_env
import kicad_import as ki
import kicad_import_d0 as kd0
import kicad_import_fplib as kf
import kicad_import_pcb as kp
import kicad_import_sch as ks

TESTS = Path(__file__).resolve().parent
FIX = TESTS / "fixtures" / "kicad" / "kimport"
EQUIV = TESTS / "fixtures" / "equiv"
PCB, XML = FIX / "kimport.kicad_pcb", FIX / "kimport.xml"
TOOLS = TESTS.parent / "server" / "tools"
K11_FP = 20260901


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _install(version: str, python: str | Path | None = "py") -> types.SimpleNamespace:
    """A stand-in kicad_env.KiCadInstall."""
    return types.SimpleNamespace(
        version=version, version_tuple=tuple(int(x) for x in version.split(".")),
        python=Path(python) if python else None, python_error=None if python else "no pcbnew")


@pytest.fixture(autouse=True)
def no_kicad(monkeypatch):
    """No real KiCad: no installs and a recorded, stubbed KiCad 9 load check."""
    checked: list[Path] = []

    def verify(path: Path, kind: str = "fp") -> dict:
        checked.append(Path(path))
        return {"ok": True, "loaded": len(list(Path(path).glob("*.kicad_mod"))), "compared": 1,
                "differing": [], "missing": [], "failed_items": [], "errors": []}
    monkeypatch.setattr(kd0, "verify", verify)
    monkeypatch.setattr(ks, "verify_symbols", lambda path: verify(path, "sym"))
    monkeypatch.setattr(kicad_env, "discover", lambda refresh=False: [])
    return checked


_FAKE_SWIG = """
import json, os, sys
board, plan, out = sys.argv[1:4]
if os.environ.get("FAKE_FP_FAIL"):
    print(json.dumps({"ok": False, "pcbnew": "fake", "error": "LoadBoard failed"}))
    sys.exit(1)
fmt = os.environ.get("FAKE_FP_FORMAT", "20241229")
extra = os.environ.get("FAKE_FP_EXTRA", "")
for e in json.load(open(plan))["footprints"]:
    lib = os.path.join(out, e["key"] + ".pretty")
    os.makedirs(lib, exist_ok=True)
    with open(os.path.join(lib, e["key"] + ".kicad_mod"), "w") as f:
        f.write('(footprint "%s" (version %s) (generator "pcbnew") (layer "F.Cu") '
                '(property "Reference" "REF**") (property "Value" "%s") %s'
                '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")))\\n'
                % (e["key"], fmt, e["key"], extra))
print(json.dumps({"ok": True, "pcbnew": os.environ.get("FAKE_PCBNEW", "9.0.4-fake"),
                  "saved": [], "unmatched": [], "warnings": []}))
"""


@pytest.fixture
def fake_swig(tmp_path: Path, monkeypatch) -> Path:
    script = tmp_path / "fake_swig.py"
    script.write_text(_FAKE_SWIG, encoding="utf-8")
    monkeypatch.setattr(kf, "SWIG_SCRIPT", script)
    return script


def _board(tmp_path: Path, source: Path, version: int | None = None,
           name: str = "b.kicad_pcb") -> tuple[Path, kp.BoardDesign]:
    """A copy of a fixture board in tmp_path (its format set to `version`)."""
    text = _text(source)
    if version is not None:
        text = re.sub(r"\(version \d+\)", f"(version {version})", text, count=1)
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path, kp.read_board(text)


def _item(name: str, pads: list[str], version: int = K11_FP, extra: str = "") -> str:
    """A library footprint as the KiCad 11 importer writes one."""
    body = "".join(f'\t(pad "{n}" smd rect (at {i} 0) (size 1 1) (layers "F.Cu"))\n'
                   for i, n in enumerate(pads))
    return (f'(footprint "{name}"\n\t(version {version})\n\t(generator "pcbnew")\n'
            f'\t(generator_version "10.99")\n\t(layer "F.Cu")\n\t(descr "{name} footprint")\n'
            f'\t(property "Reference" "REF**" (at 0 -1 0) (layer "F.SilkS"))\n'
            f'\t(property "Value" "{name}" (at 0 1 0) (layer "F.Fab"))\n'
            f'\t(duplicate_pad_numbers_are_jumpers no)\n{extra}{body})\n')


def _library(path: Path, board: kp.BoardDesign, **extra: str) -> Path:
    """A .pretty holding one item per board footprint name, its pads those of
    the board instance (``extra``: {item name: text added to it})."""
    path.mkdir(parents=True, exist_ok=True)
    for fp in board.footprints:
        (path / f"{fp.item_name}.kicad_mod").write_text(
            _item(fp.item_name, [p.number for p in fp.pads], extra=extra.get(fp.item_name, "")),
            encoding="utf-8")
    return path


def _provide(path: Path, board: kp.BoardDesign, out: Path, **kw):
    return kd0.provide_footprints(path, board, "n", out / "n.pretty", **kw)


# ----------------------------------------------------------------------------
# Route a: a KiCad 9 board, today's extraction
# ----------------------------------------------------------------------------
def test_kicad9_board_takes_route_pcbnew9_and_is_checked_by_kicad9(
        tmp_path: Path, fake_swig: Path, no_kicad: list) -> None:
    board = kp.read_board(_text(PCB))
    lib, info = _provide(PCB, board, tmp_path, kicad_python=sys.executable)
    assert info["route"] == "pcbnew9" and info["extracted"] is True
    assert info["format"] == kf.K9_PCB_FORMAT and info["d0_dialect"] is True
    assert info["k9_verified"] is True and no_kicad == [tmp_path / "n.pretty"]
    assert info["refused"] == [] and info["python"] == sys.executable
    assert set(lib.names) == {fp.index for fp in board.footprints}
    assert info["routes_tried"] == []                     # the first route worked


def test_route_pcbnew9_in_a_kicad10_only_image_reports_the_dialect_as_today(
        tmp_path: Path, fake_swig: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_FP_FORMAT", "20260206")
    board = kp.read_board(_text(PCB))
    _lib, info = _provide(PCB, board, tmp_path, kicad_python=sys.executable)
    assert info["route"] == "pcbnew9" and info["format"] == 20260206
    assert info["d0_dialect"] is False
    assert any("KiCad 9 cannot load" in w for w in info["warnings"])


def test_extraction_turned_off_records_why(tmp_path: Path) -> None:
    lib, info = _provide(PCB, kp.read_board(_text(PCB)), tmp_path, enabled=False)
    assert lib is None and info["extracted"] is False and info["route"] is None
    assert info["reason"] == "footprint extraction was turned off"


def test_a_failing_pcbnew_fails_loudly_when_no_other_route_works(
        tmp_path: Path, fake_swig: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_FP_FAIL", "1")
    with pytest.raises(RuntimeError, match="LoadBoard failed"):
        _provide(PCB, kp.read_board(_text(PCB)), tmp_path, kicad_python=sys.executable)


def test_a_failing_pcbnew_falls_back_to_a_later_route_and_says_so(
        tmp_path: Path, fake_swig: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_FP_FAIL", "1")
    path, board = _board(tmp_path, PCB)
    lib, info = _provide(path, board, tmp_path / "out", kicad_python=sys.executable,
                         sources=[_library(tmp_path / "src.pretty", board)])
    assert lib is not None and info["route"] == "importer_library"
    assert info["routes_tried"][0]["route"] == "pcbnew9"
    assert "LoadBoard failed" in info["routes_tried"][0]["reason"]
    assert any("LoadBoard failed" in w for w in info["warnings"])


# ----------------------------------------------------------------------------
# Route b: a KiCad 10 board, pcbnew >= 10 + kicad_dialect
# ----------------------------------------------------------------------------
def test_kicad10_board_is_extracted_by_pcbnew10_and_downgraded(
        tmp_path: Path, fake_swig: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_FP_FORMAT", "20260206")
    monkeypatch.setenv("FAKE_PCBNEW", "10.0.5")
    monkeypatch.setenv("FAKE_FP_EXTRA", "(duplicate_pad_numbers_are_jumpers no) ")
    k9, k10 = _install("9.0.4", tmp_path / "no-such-k9-python"), _install("10.0.5", sys.executable)
    monkeypatch.setattr(kicad_env, "discover", lambda refresh=False: [k9, k10])
    path, board = _board(tmp_path, EQUIV / "board_k10.sexpr")
    lib, info = _provide(path, board, tmp_path)
    assert info["route"] == "pcbnew10+downgrade" and info["python"] == sys.executable
    assert info["format"] == kf.K9_PCB_FORMAT and info["d0_dialect"] is True
    assert info["pcbnew"] == "10.0.5" and info["k9_verified"] is True
    texts = [_text(Path(f)) for f in lib.files]
    assert all("(version 20241229)" in t and "jumpers" not in t for t in texts)
    assert info["downgrade"]["dropped"] == {        # counted per footprint downgraded
        "(duplicate_pad_numbers_are_jumpers no)": len(board.footprints)}
    assert [r["route"] for r in info["routes_tried"]] == ["pcbnew9"]
    assert "newer than KiCad 9" in info["routes_tried"][0]["reason"]


def test_a_refused_construct_fails_route_b_and_is_reported(
        tmp_path: Path, fake_swig: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_FP_FORMAT", "20260206")
    monkeypatch.setenv("FAKE_FP_EXTRA", '(jumper_pad_groups ("1" "2")) ')
    path, board = _board(tmp_path, EQUIV / "board_k10.sexpr")
    lib, info = _provide(path, board, tmp_path, kicad_python=sys.executable)
    assert lib is None and info["extracted"] is False and info["route"] is None
    assert info["kicad11_path_required"] is True
    assert info["refused"] and {r["construct"] for r in info["refused"]} == {
        "(jumper_pad_groups ...)"}
    tried = {r["route"]: r["reason"] for r in info["routes_tried"]}
    assert list(tried) == list(kd0.ROUTES)
    assert "refused" in tried["pcbnew10+downgrade"] and tried["pcbnew10+downgrade"] in \
        info["reason"]
    assert not list((tmp_path / "n.pretty").glob("*.kicad_mod"))      # nothing half-written


def test_route_b_needs_a_pcbnew_that_reads_the_board(tmp_path: Path, monkeypatch) -> None:
    """KiCad 10.0 reads boards up to 20260206; a 10.99 board needs a 10.99
    pcbnew, and KiCad 9 reads neither."""
    k9, k10, k11 = (_install("9.0.4"), _install("10.0.5"), _install("10.99.0"))
    assert kd0.choose_newer_install(20260206, [k9, k10, k11]) is k10      # the oldest able
    assert kd0.choose_newer_install(K11_FP, [k9, k10, k11]) is k11
    assert kd0.choose_newer_install(K11_FP, [k9, k10]) is None
    assert kd0.choose_newer_install(20260206, [k9, _install("10.0.5", None)]) is None
    monkeypatch.setattr(kicad_env, "discover", lambda refresh=False: [k9, k10])
    path, board = _board(tmp_path, EQUIV / "board_k11.sexpr")
    lib, info = _provide(path, board, tmp_path)
    tried = {r["route"]: r["reason"] for r in info["routes_tried"]}
    assert lib is None and "10.99" in tried["pcbnew10+downgrade"]
    assert "KiCad 10.0.5" in tried["pcbnew10+downgrade"]


# ----------------------------------------------------------------------------
# Route c: the KiCad 11 importer's footprint library
# ----------------------------------------------------------------------------
def test_importer_library_is_downgraded_and_its_pads_verified(tmp_path: Path) -> None:
    path, board = _board(tmp_path, EQUIV / "board_k11.sexpr")
    src = _library(tmp_path / "src-import-fps.pretty", board)
    lib, info = _provide(path, board, tmp_path / "out", sources=[src])
    assert info["route"] == "importer_library" and info["extracted"] is True
    assert info["format"] == kf.K9_PCB_FORMAT and info["d0_dialect"] is True
    assert info["k9_verified"] is True and info["sources"] == [str(src)]
    by_ref = {fp.ref: lib.names[fp.index] for fp in board.footprints}
    assert by_ref["R1"] == by_ref["R2"] == "n:R_0603_1608Metric"           # one item, shared
    assert by_ref["U1"] == "n:DFN-6-1EP_2x2mm_P0.65mm"
    assert len(lib.files) == info["footprints"] == 5 and info["variants"] == []
    for f in map(Path, lib.files):
        text = _text(f)
        assert "(version 20241229)" in text and "jumpers" not in text
        assert f'(footprint "{f.stem}"' in text
    assert [r["route"] for r in info["routes_tried"]] == ["pcbnew9", "pcbnew10+downgrade"]


def test_importer_library_must_match_the_board_pads(tmp_path: Path) -> None:
    path, board = _board(tmp_path, EQUIV / "board_k11.sexpr")
    src = _library(tmp_path / "src.pretty", board)
    (src / "DFN-6-1EP_2x2mm_P0.65mm.kicad_mod").write_text(
        _item("DFN-6-1EP_2x2mm_P0.65mm", ["1", "2", "3", "4", "EP"]), encoding="utf-8")
    lib, info = _provide(path, board, tmp_path / "out", sources=[src])
    reason = {r["route"]: r["reason"] for r in info["routes_tried"]}["importer_library"]
    assert lib is None and "U1" in reason and "EP" in reason and "pads" in reason


def test_importer_library_must_hold_every_board_footprint(tmp_path: Path) -> None:
    path, board = _board(tmp_path, EQUIV / "board_k11.sexpr")
    src = _library(tmp_path / "src.pretty", board)
    (src / "MountingHole_3.2mm_M3_Pad.kicad_mod").unlink()
    lib, info = _provide(path, board, tmp_path / "out", sources=[src, tmp_path / "nope.pretty"])
    reason = {r["route"]: r["reason"] for r in info["routes_tried"]}["importer_library"]
    assert lib is None and "MountingHole_3.2mm_M3_Pad" in reason and "H1" in reason
    assert "nope.pretty" in reason


def test_importer_library_prefers_the_directory_named_like_the_nickname(tmp_path: Path) -> None:
    path, board = _board(tmp_path, EQUIV / "board_k11.sexpr")
    other = _library(tmp_path / "Other.pretty", board, R_0603_1608Metric=(
        '\t(fp_circle (center 0 0) (end 1 0) (layer "F.SilkS"))\n'))
    named = _library(tmp_path / "Resistor_SMD.pretty", board)
    lib, info = _provide(path, board, tmp_path / "out", sources=[other, named])
    r1 = next(fp for fp in board.footprints if fp.ref == "R1")
    assert "fp_circle" not in _text(tmp_path / "out" / "n.pretty"
                                     / f"{lib.names[r1.index].split(':')[1]}.kicad_mod")


def test_importer_library_refusals_are_reported(tmp_path: Path) -> None:
    path, board = _board(tmp_path, EQUIV / "board_k11.sexpr")
    src = _library(tmp_path / "src.pretty", board, **{
        "PinHeader_1x02_P2.54mm_Vertical": '\t(jumper_pad_groups ("1" "2"))\n'})
    lib, info = _provide(path, board, tmp_path / "out", sources=[src])
    assert lib is None
    assert info["refused"] == [{"route": "importer_library",
                                "item": "PinHeader_1x02_P2.54mm_Vertical",
                                "construct": "(jumper_pad_groups ...)",
                                "reason": "footprint 'PinHeader_1x02_P2.54mm_Vertical': "
                                          "(jumper_pad_groups ...): pad groups jumpered inside "
                                          "the part (KiCad 10); KiCad 9 has no form for it"}]


# ----------------------------------------------------------------------------
# Route d: the source project's own footprint libraries
# ----------------------------------------------------------------------------
def _fp_lib_table(root: Path, *names: str) -> None:
    rows = "".join(f'  (lib (name "{n}") (type "KiCad") (uri "${{KIPRJMOD}}/{n}.pretty") '
                   f'(options "") (descr ""))\n' for n in names)
    (root / "fp-lib-table").write_text(f"(fp_lib_table (version 7)\n{rows})\n", encoding="utf-8")


def test_project_tables_copy_the_used_items_and_name_clashes_get_variants(tmp_path: Path) -> None:
    """C1 uses Capacitor_SMD:R_0603_1608Metric, another footprint than
    Resistor_SMD's of that name (and already in KiCad 9's dialect): one
    library, so it becomes R_0603_1608Metric_2, as an extraction names it."""
    text = _text(EQUIV / "board_k11.sexpr").replace('"Capacitor_SMD:C_0603_1608Metric"',
                                                    '"Capacitor_SMD:R_0603_1608Metric"')
    path = tmp_path / "b.kicad_pcb"
    path.write_text(text, encoding="utf-8")
    board = kp.read_board(text)
    nicks = sorted({fp.lib_id.partition(":")[0] for fp in board.footprints})
    for nick in nicks:
        _library(tmp_path / f"{nick}.pretty", board)
    (tmp_path / "Resistor_SMD.pretty" / "unused.kicad_mod").write_text(_item("unused", ["1"]))
    (tmp_path / "Capacitor_SMD.pretty" / "R_0603_1608Metric.kicad_mod").write_text(
        _item("R_0603_1608Metric", ["1", "2"], version=kf.K9_PCB_FORMAT,
              extra='\t(fp_line (start 0 0) (end 1 0) (layer "F.SilkS"))\n'))
    _fp_lib_table(tmp_path, *nicks)
    lib, info = _provide(path, board, tmp_path / "out")
    assert info["route"] == "project_tables" and info["format"] == kf.K9_PCB_FORMAT
    names = {fp.ref: lib.names[fp.index] for fp in board.footprints}
    assert names["R1"] == names["R2"] == "n:R_0603_1608Metric"
    assert names["C1"] == "n:R_0603_1608Metric_2"
    assert [v["name"] for v in info["variants"]] == ["R_0603_1608Metric_2"]
    out = tmp_path / "out" / "n.pretty"
    assert "fp_line" in _text(out / "R_0603_1608Metric_2.kicad_mod")
    assert "fp_line" not in _text(out / "R_0603_1608Metric.kicad_mod")
    assert not (out / "unused.kicad_mod").exists() and len(lib.files) == 5
    assert info["sources"] == [str((tmp_path / f"{n}.pretty").resolve()) for n in nicks]


def test_project_tables_take_kicad5_module_files_as_they_are(tmp_path: Path) -> None:
    """A KiCad 5 ``(module ...)`` item (bare pad numbers, no version) loads in
    KiCad 9 as it is: copied unchanged but for its name."""
    path, board = _board(tmp_path, EQUIV / "board_k11.sexpr")
    nicks = sorted({fp.lib_id.partition(":")[0] for fp in board.footprints})
    for nick in nicks:
        _library(tmp_path / f"{nick}.pretty", board)
    legacy = ('(module R_0603_old (layer F.Cu) (tedit 5B307E8C)\n'
              '  (fp_text reference REF** (at 0 -1.4) (layer F.SilkS))\n'
              '  (pad 1 smd rect (at -0.8 0) (size 0.8 0.9) (layers F.Cu F.Paste F.Mask))\n'
              '  (pad 2 smd rect (at 0.8 0) (size 0.8 0.9) (layers F.Cu F.Paste F.Mask)))\n')
    (tmp_path / "Resistor_SMD.pretty" / "R_0603_1608Metric.kicad_mod").write_text(legacy)
    _fp_lib_table(tmp_path, *nicks)
    lib, info = _provide(path, board, tmp_path / "out")
    assert info["route"] == "project_tables" and info["d0_dialect"] is True
    r1 = next(fp for fp in board.footprints if fp.ref == "R1")
    copied = _text(tmp_path / "out" / "n.pretty" / "R_0603_1608Metric.kicad_mod")
    assert lib.names[r1.index] == "n:R_0603_1608Metric"
    assert copied == legacy.replace("(module R_0603_old", '(module "R_0603_1608Metric"')


def test_nothing_works_for_a_newer_board_and_every_reason_is_kept(tmp_path: Path) -> None:
    path, board = _board(tmp_path, EQUIV / "board_k11.sexpr")
    lib, info = _provide(path, board, tmp_path / "out")
    assert lib is None and info["extracted"] is False and info["kicad11_path_required"] is True
    assert [r["route"] for r in info["routes_tried"]] == list(kd0.ROUTES)
    assert "no footprint_sources" in info["reason"] and "fp-lib-table" in info["reason"]
    assert info["refused"] == []


def test_a_kicad9_board_without_any_route_is_not_sent_to_kicad11(tmp_path: Path) -> None:
    path, board = _board(tmp_path, PCB)
    lib, info = _provide(path, board, tmp_path / "out")
    assert lib is None and "kicad11_path_required" not in info
    assert "no KiCad >= 9.0" in info["reason"]


# ----------------------------------------------------------------------------
# 3D models and pad tables
# ----------------------------------------------------------------------------
_MODELS = ('(footprint "X" (version 20241229) (layer "F.Cu")\n'
           '\t(model "${KIPRJMOD}/3d/part.step" (offset (xyz 0 0 0)))\n'
           '\t(model "${KIPRJMOD}/3d/gone.step" (offset (xyz 0 0 0)))\n'
           '\t(model "3d/rel.wrl")\n'
           '\t(model "Sockets.3dshapes/PGA120.wrl")\n'
           '\t(model "kicad-embed://SODFL1608X65N.step")\n'
           '\t(model "${KICAD9_3DMODEL_DIR}/R.3dshapes/R.step")\n'
           '\t(model "C:/abs/x.step")\n'
           '\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")))\n')


def test_project_relative_models_point_into_the_source_project_or_are_dropped(
        tmp_path: Path) -> None:
    """A missing ${KIPRJMOD} model goes; a missing plain relative one (KiCad
    5 libraries: KiCad's 3D search paths), an embedded one, a stock-variable
    one and an absolute one stay as they are."""
    (tmp_path / "3d").mkdir()
    (tmp_path / "3d" / "part.step").write_text("x")
    (tmp_path / "3d" / "rel.wrl").write_text("x")
    edit = kd0.rewrite_models(_MODELS, tmp_path)
    assert edit.rewritten == ("${KIPRJMOD}/3d/part.step", "3d/rel.wrl")
    assert edit.dropped == ("${KIPRJMOD}/3d/gone.step",)
    models = re.findall(r'\(model "([^"]+)"', edit.text)
    assert models == [(tmp_path / "3d" / "part.step").as_posix(),
                      (tmp_path / "3d" / "rel.wrl").as_posix(),
                      "Sockets.3dshapes/PGA120.wrl", "kicad-embed://SODFL1608X65N.step",
                      "${KICAD9_3DMODEL_DIR}/R.3dshapes/R.step", "C:/abs/x.step"]
    assert "gone" not in edit.text and edit.text.count("\t(model") == 6
    assert kd0.rewrite_models(edit.text, tmp_path).text == edit.text        # idempotent


def test_dropped_models_are_counted_in_the_report(tmp_path: Path, fake_swig: Path,
                                                  monkeypatch) -> None:
    """Counted per model file: every footprint names the same missing one."""
    monkeypatch.setenv("FAKE_FP_EXTRA", '(model "${KIPRJMOD}/3d/none.step") ')
    lib, info = _provide(PCB, kp.read_board(_text(PCB)), tmp_path, kicad_python=sys.executable)
    assert (info["models_dropped"], info["models_rewritten"]) == (1, 0)
    assert info["models_missing"] == ["${KIPRJMOD}/3d/none.step"]
    assert not any("(model" in _text(Path(f)) for f in lib.files)


def test_numbered_pads_read_like_the_board_reader() -> None:
    text = ('(footprint "X" (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))\n'
            ' (pad "1" smd rect (at 1 0) (size 1 1) (layers "F.Cu"))\n'
            ' (pad "" np_thru_hole circle (at 2 0) (size 1 1) (drill 1) (layers "*.Cu"))\n'
            ' (pad "7" np_thru_hole circle (at 3 0) (size 1 1) (drill 1) (layers "*.Cu"))\n'
            ' (pad "A" smd rect (at 4 0) (size 1 1) (layers "F.Paste")))\n')
    assert kd0.numbered_pads(text) == {"1": 2}


# ----------------------------------------------------------------------------
# Symbols (kicad_import_sch.write_symbol_library)
# ----------------------------------------------------------------------------
def _k11_sheet(extra: str = "") -> list[tuple[str, str]]:
    sym = ('(symbol "Lib:R" (in_bom yes) (on_board yes) (in_pos_files yes)\n'
           '\t\t\t(duplicate_pin_numbers_are_jumpers no) ' + extra +
           '(property "Reference" "R" (at 0 0 0) (effects (font (size 1 1))))\n'
           '\t\t\t(symbol "R_1_1" (pin passive line (at 0 3.81 270) (length 1.27)\n'
           '\t\t\t\t(name "~" (effects (font (size 1 1)))) (number "1" (effects (font '
           '(size 1 1)))))))')
    return [("k.kicad_sch", f'(kicad_sch (version 20260830) (generator "eeschema")\n'
                            f'\t(lib_symbols\n\t\t{sym}\n\t)\n)\n')]


def _sch_comp() -> ks.SchComponent:
    return ks.SchComponent(ref="R1", value="1k", footprint="", datasheet="", description="",
                           lib="Lib", part="R", fields=(), flags=frozenset(),
                           sheet_names="/", sheet_file="k.kicad_sch")


def test_symbols_of_a_newer_schematic_are_written_in_kicad9_dialect(
        tmp_path: Path, no_kicad: list) -> None:
    path = tmp_path / "k.kicad_sym"
    plan, info = ks.write_symbol_library([_sch_comp()], _k11_sheet(), path)
    text = _text(path)
    assert info["format"] == ks.K9_SYM_FORMAT and info["d0_dialect"] is True
    assert "(version 20241209)" in text and "jumpers" not in text and "in_pos_files" not in text
    assert info["downgraded_from"] == ks.K10_SYM_FORMAT and "kicad11_path_required" not in info
    assert info["k9_verified"] is True and no_kicad == [path]
    assert plan.names == {"R1": "R"}


def test_a_refused_symbol_keeps_the_newer_library_and_names_the_construct(
        tmp_path: Path) -> None:
    path = tmp_path / "k.kicad_sym"
    _plan, info = ks.write_symbol_library(
        [_sch_comp()], _k11_sheet('(jumper_pin_groups ("1" "2")) '), path)
    assert info["format"] == ks.K10_SYM_FORMAT and info["d0_dialect"] is False
    assert f"(version {ks.K10_SYM_FORMAT})" in _text(path) and "jumper_pin_groups" in _text(path)
    assert "(jumper_pin_groups ...)" in info["kicad11_path_required"]
    assert info["refused"] == [{"item": "R", "construct": "(jumper_pin_groups ...)",
                                "reason": "pin groups jumpered inside the part (KiCad 10); "
                                          "KiCad 9 has no form for it"}]
    assert "k9_verified" not in info


def test_symbols_of_a_kicad9_schematic_are_written_as_they_are(tmp_path: Path,
                                                             no_kicad: list) -> None:
    sch = ks.parse_netlist_xml(_text(XML))
    _plan, info = ks.write_symbol_library(
        sch.components[:1], ks.read_sheets(FIX / "kimport.kicad_sch"), tmp_path / "k.kicad_sym")
    assert info["format"] == ks.K9_SYM_FORMAT and info["d0_dialect"] is True
    assert no_kicad == [] and "k9_verified" not in info


# ----------------------------------------------------------------------------
# convert(): footprint_sources is passed through
# ----------------------------------------------------------------------------
def _k11_project(tmp_path: Path) -> tuple[Path, Path]:
    """The kimport fixture project with its board stamped KiCad 11, and an
    importer-style library of its footprints."""
    project = tmp_path / "kimport"
    project.mkdir()
    for name in ("kimport.kicad_pro", "kimport.kicad_sch"):
        shutil.copyfile(FIX / name, project / name)
    _path, board = _board(project, PCB, version=K11_FP, name="kimport.kicad_pcb")
    return project, _library(tmp_path / "kimport-import-fps.pretty", board)


def test_convert_passes_footprint_sources_to_the_importer_library_route(tmp_path: Path) -> None:
    project, src = _k11_project(tmp_path)
    res = ki.convert(project, tmp_path / "out", netlist_xml=str(XML), footprint_sources=[src])
    assert res["ok"], json.dumps(res["report"]["checks"])[:1500]
    info = res["report"]["libraries"]["footprints"]
    assert info["route"] == "importer_library" and info["format"] == kf.K9_PCB_FORMAT
    design = res["design"]
    assert design["libraries"]["footprints"] == {"kimport_imported": "libs/kimport_imported.pretty"}
    assert all(c["footprint"].startswith("kimport_imported:") for c in design["components"])
    out = ki.summary(res)
    assert out["footprints_extracted"] is True and out["footprint_route"] == "importer_library"


def test_cli_takes_footprint_sources(tmp_path: Path, capsys) -> None:
    project, src = _k11_project(tmp_path)
    rc = ki.main([str(project), str(tmp_path / "out"), "--netlist-xml", str(XML),
                  "--footprint-source", str(src)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["footprint_route"] == "importer_library"


def test_no_module_uses_net_equivalence_private_names() -> None:
    """kicad_import* read through sexpr_read's public names (F46-01)."""
    private = re.compile(r"\bne\._")
    users = [p.name for p in TOOLS.glob("*.py")
             if p.name != "net_equivalence.py" and private.search(_text(p))]
    assert users == []
