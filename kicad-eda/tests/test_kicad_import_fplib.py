"""server/tools/kicad_import_fplib.py: the footprint library of the KiCad
project importer (ROADMAP D4), unit tier (no KiCad).

Footprint identity and naming, instance-field stripping, the choice of the
pcbnew that extracts (the oldest >= 9.0, D0) and of the kicad-cli that
exports, the extraction driver run against a stand-in for
kicad_import_swig.py (one library directory per footprint, the dialect it
wrote), and the project's own footprint libraries. The real pcbnew runs in
tests/test_kicad_import_live.py.
"""
from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

import kicad_env
import kicad_import_fplib as kf
import kicad_import_pcb as kp
import kicad_import_sch as ks

FIX = Path(__file__).resolve().parent / "fixtures" / "kicad" / "kimport"
EQUIV = Path(__file__).resolve().parent / "fixtures" / "equiv"
PCB = FIX / "kimport.kicad_pcb"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _mini_board(*items: str) -> str:
    return ('(kicad_pcb (version 20241229) (generator "pcbnew") (generator_version "9.0")'
            ' (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user)) (net 0 "") '
            + " ".join(items) + ")")


_FP = ('(footprint "{name}" (version 20241229) (layer "F.Cu")\n'
       '\t(property "Reference" "REF**" (at {rx} -1.43 0) (layer "F.SilkS") (uuid "{u1}"))\n'
       '\t(property "Value" "{name}" (at 0 1.43 0) (layer "F.Fab") (uuid "{u2}"))\n'
       '\t(fp_text user "${{REFERENCE}}" (at {tx} 0 {ta}) (layer "F.Fab"))\n'
       '\t(fp_line (start -0.14058 -0.51) (end {lx} -0.51) (layer "F.SilkS") (uuid "{u3}"))\n'
       '\t(pad "1" smd roundrect (at -0.775 0) (size {px} 0.95) (layers "F.Cu")))\n')


def _fp(**kw) -> str:
    base = dict(name="fp00001", rx=0, u1="a", u2="b", tx=0, ta=0, lx="0.14058", u3="c", px="0.9")
    return _FP.format(**{**base, **kw})


def test_footprint_identity_ignores_instance_noise_but_not_geometry() -> None:
    first = kf.footprint_identity(_fp())
    assert kf.footprint_identity(_fp(name="fp00002", rx=1.5, u1="x", u2="y", u3="z",
                                     tx=0.3, ta=180, lx="0.140581")) == first
    assert kf.footprint_identity(_fp(px="1.0")) != first


def test_footprint_identity_ignores_the_order_items_are_written_in() -> None:
    """pcbnew 9 writes a footprint's texts in an order that follows where
    each instance's texts ended up (vme-wren, complex_hierarchy): the same
    footprint either way. Different items still differ."""
    two_texts = _fp().replace('\t(fp_line', '\t(fp_text user "K" (at 1 2 0) (layer "F.Fab"))\n'
                                            '\t(fp_line')
    lines = two_texts.splitlines(keepends=True)
    k_line = next(i for i, ln in enumerate(lines) if '"K"' in ln)
    ref_line = next(i for i, ln in enumerate(lines) if "${REFERENCE}" in ln)
    lines[k_line], lines[ref_line] = lines[ref_line], lines[k_line]
    swapped = "".join(lines)
    assert swapped != two_texts
    assert kf.footprint_identity(swapped) == kf.footprint_identity(two_texts)
    assert kf.footprint_identity(two_texts.replace('"K"', '"A"')) != \
        kf.footprint_identity(two_texts)
    assert kf.footprint_identity(two_texts) != kf.footprint_identity(_fp())


def test_instance_fields_are_stripped_from_saved_footprints() -> None:
    clean = _fp()
    anchor = '\t(fp_text user'
    noisy = clean.replace(anchor, '\t(property "MPN" "RC0603\\"x" (at 0 0 0) (layer "F.Fab") '
                                  '(hide yes))\n\t(property "Sim.Device" "R" (at 0 0 0))\n'
                          + anchor)
    assert '"MPN"' in noisy
    assert kf.strip_instance_fields(noisy) == clean        # with the whitespace before each
    assert kf.strip_instance_fields(clean) == clean


def _k10_saved(sheet: str, sheet_file: str, **kw) -> str:
    """A footprint as pcbnew 10's FootprintSave writes one taken from a board:
    the symbol's footprint filters (an UNQUOTED ki_ property name), the
    sheet it came from, and (as the board stores it) the symbol path."""
    anchor = '\t(fp_text user'
    return _fp(**kw).replace(anchor, (
        '\t(property ki_fp_filters "R_* Resistor_*")\n'
        '\t(property "ki_keywords" "R res")\n'
        f'\t(path "{sheet}4ae173cf-0000-4000-8000-000000000001")\n'
        f'\t(sheetname "{sheet}")\n\t(sheetfile "{sheet_file}")\n') + anchor)


def test_kicad10_instance_data_is_stripped_from_saved_footprints() -> None:
    """pcbnew 10 keeps the sheet a footprint came from and the symbol's
    footprint filters in a library copy (KiCad 9 drops both): two copies
    from different sheets must strip to the same footprint."""
    a = _k10_saved("/ampli_ht_vertical/", "ampli_ht.kicad_sch")
    b = _k10_saved("/", "complex_hierarchy.kicad_sch", name="fp00002", u1="x", u2="y")
    assert "(property ki_fp_filters" in a and "(sheetname" in a and "(path" in a
    assert kf.strip_instance_fields(a) == _fp()
    assert kf.footprint_identity(kf.strip_instance_fields(a)) == \
        kf.footprint_identity(kf.strip_instance_fields(b))
    assert kf.footprint_identity(a) != kf.footprint_identity(b)     # unstripped they differ


def test_only_library_fields_survive_stripping() -> None:
    names = re.compile(r'\(\s*property\s+("?)([^"\s()]+)\1')
    noisy = _k10_saved("/a/", "a.kicad_sch").replace(
        '\t(fp_text user', '\t(property "Footprint" "Lib:R" (at 0 0 0))\n'
                           '\t(property "Datasheet" "~" (at 0 0 0))\n\t(fp_text user')
    kept = [m.group(2) for m in names.finditer(kf.strip_instance_fields(noisy))]
    assert kept == ["Reference", "Value", "Footprint", "Datasheet"]
    assert set(kept) <= kf.KEEP_FIELDS


def test_identical_footprints_share_a_name_and_variants_are_numbered() -> None:
    def fp(index: int, side: str = "F", rot: float = 0.0) -> kp.BoardFootprint:
        return kp.BoardFootprint(index, f"R{index}", "Lib:R_0603", 0, 0, rot, side, "", frozenset(),
                                 (), "")
    saved = [(fp(0, "B", 45.0), _fp(lx="0.140581")), (fp(1), _fp()), (fp(2), _fp(px="1.2"))]
    names, texts, variants = kf._assign_names(saved)
    assert names == {0: "R_0603", 1: "R_0603", 2: "R_0603_2"}
    assert "(end 0.14058 -0.51)" in texts["R_0603"]      # the exact F-side copy is kept
    assert [v["name"] for v in variants] == ["R_0603_2"]


def test_extraction_refuses_boards_newer_than_kicad9(tmp_path: Path) -> None:
    board = kp.read_board(_text(EQUIV / "board_k11.sexpr"))
    with pytest.raises(ValueError, match="KiCad 11 path"):
        kf.extract_footprint_library(tmp_path / "x.kicad_pcb", board, board.footprints, "n",
                                     tmp_path / "n.pretty", "python")
    lib, info = kf.write_footprint_library(tmp_path / "x.kicad_pcb", board, [], "n",
                                           tmp_path / "n.pretty", "python", True)
    assert lib is None and info["kicad11_path_required"] is True


def _install(version: str, python: str | Path | None = "py") -> types.SimpleNamespace:
    """A stand-in kicad_env.KiCadInstall."""
    return types.SimpleNamespace(
        version=version, version_tuple=tuple(int(x) for x in version.split(".")),
        python=Path(python) if python else None, python_error=None if python else "no pcbnew")


def test_footprints_are_extracted_by_the_oldest_pcbnew_that_reads_the_board() -> None:
    nightly, k10, k9, k8 = (_install("10.99.0", None), _install("10.0.5"), _install("9.0.4"),
                            _install("8.0.9"))
    every = [nightly, k10, k9, k8]
    assert kf.choose_swig_install(kf.K9_PCB_FORMAT, every) is k9      # D0: not the newest
    assert kf.choose_swig_install(20250907, every) is k10             # a KiCad 10 board
    assert kf.choose_swig_install(kf.K9_PCB_FORMAT, [nightly, k10]) is k10    # Docker/CI
    assert kf.choose_swig_install(kf.K9_PCB_FORMAT, [nightly, k8]) is None


_FAKE_SWIG = """
import json, os, sys
board, plan, out = sys.argv[1:4]
fmt = os.environ.get("FAKE_FP_FORMAT", "20241229")
if os.environ.get("FAKE_FP_FAIL"):
    print(json.dumps({"ok": False, "pcbnew": "fake", "error": "LoadBoard failed"}))
    sys.exit(1)
same = bool(os.environ.get("FAKE_FP_SAME"))         # one geometry, a sheet per footprint
for n, e in enumerate(json.load(open(plan))["footprints"]):
    lib = os.path.join(out, e["key"] + ".pretty")
    os.makedirs(lib, exist_ok=True)
    sheet = ('\\n\\t(property ki_fp_filters "F%d*")\\n\\t(sheetname "/s%d/")\\n'
             '\\t(sheetfile "s%d.kicad_sch")' % (n, n, n)) if same else ""
    with open(os.path.join(lib, e["key"] + ".kicad_mod"), "w") as f:
        f.write('(footprint "%s" (version %s) (layer "F.Cu") (property "Reference" "REF**") '
                '(property "Value" "%s")%s (pad "1" smd rect (at 0 0) (size %d 1) '
                '(layers "F.Cu")))' % (e["key"], fmt, e["key"], sheet,
                                        1 if same else int(e["key"][2:]) + 1))
print(json.dumps({"ok": True, "pcbnew": "9.0.4-fake", "saved": [], "unmatched": [],
                  "warnings": []}))
"""


@pytest.fixture
def fake_swig(tmp_path: Path, monkeypatch) -> Path:
    """kicad_import_swig.py replaced by a stand-in that writes one library
    directory per footprint, as the real one does; run by this Python."""
    script = tmp_path / "fake_swig.py"
    script.write_text(_FAKE_SWIG, encoding="utf-8")
    monkeypatch.setattr(kf, "SWIG_SCRIPT", script)
    return script


@pytest.mark.parametrize("fmt, d0", [(kf.K9_PCB_FORMAT, True), (20260206, False)])
def test_extracted_library_records_the_dialect_pcbnew_wrote(
        tmp_path: Path, fake_swig: Path, monkeypatch, fmt: int, d0: bool) -> None:
    monkeypatch.setenv("FAKE_FP_FORMAT", str(fmt))
    board = kp.read_board(_text(PCB))
    lib, info = kf.write_footprint_library(PCB, board, [], "n", tmp_path / "n.pretty",
                                           sys.executable, True)
    assert set(lib.names) == {fp.index for fp in board.footprints}
    assert info["extracted"] and info["format"] == fmt and info["d0_dialect"] is d0
    # every footprint read back from its own directory: 9 distinct geometries
    assert len(lib.files) == info["footprints"] == len(set(lib.names.values())) == 9
    assert all(Path(f).is_file() and f"(version {fmt})" in _text(Path(f)) for f in lib.files)
    assert any("KiCad 9 cannot load" in w for w in info["warnings"]) is not d0


def test_one_footprint_placed_on_many_sheets_is_one_library_footprint(
        tmp_path: Path, fake_swig: Path, monkeypatch) -> None:
    """pcbnew 10 saves each copy with the sheet it sits on: nine copies of one
    geometry from nine sheets are still one library footprint, no variants."""
    monkeypatch.setenv("FAKE_FP_SAME", "1")
    board = kp.read_board(_text(PCB))
    lib, info = kf.write_footprint_library(PCB, board, [], "n", tmp_path / "n.pretty",
                                           sys.executable, True)
    assert len(lib.files) == info["footprints"] == 1 and info["variants"] == []
    assert len(set(lib.names.values())) == 1 and set(lib.names) == {fp.index
                                                                     for fp in board.footprints}
    text = _text(Path(lib.files[0]))
    assert "sheetname" not in text and "sheetfile" not in text and "ki_" not in text


def test_write_footprint_library_picks_the_install_itself(
        tmp_path: Path, fake_swig: Path, monkeypatch) -> None:
    k9 = _install("9.0.4", sys.executable)
    monkeypatch.setattr(kicad_env, "discover", lambda refresh=False: [
        _install("10.0.5", tmp_path / "no-such-python"), k9])
    board = kp.read_board(_text(PCB))
    _lib, info = kf.write_footprint_library(PCB, board, [], "n", tmp_path / "n.pretty", None, True)
    assert info["python"] == sys.executable                # the 9.0 install, not the 10.0 one
    monkeypatch.setattr(kicad_env, "discover", lambda refresh=False: [_install("10.99.0", None)])
    lib, info = kf.write_footprint_library(PCB, board, [], "n", tmp_path / "m.pretty", None, True)
    assert lib is None and "no KiCad >= 9.0" in info["reason"] and "10.99.0" in info["reason"]


def test_a_failed_extraction_fails_loudly(tmp_path: Path, fake_swig: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_FP_FAIL", "1")
    board = kp.read_board(_text(PCB))
    with pytest.raises(RuntimeError, match="LoadBoard failed"):
        kf.write_footprint_library(PCB, board, [], "n", tmp_path / "n.pretty", sys.executable,
                                   True)
    with pytest.raises(RuntimeError, match="cannot start KiCad's Python"):
        kf.write_footprint_library(PCB, board, [], "n", tmp_path / "n.pretty",
                                   str(tmp_path / "no-such-python.exe"), True)


def test_kicad_cli_choice_follows_the_schematic_format(monkeypatch) -> None:
    def cli(version: str) -> types.SimpleNamespace:
        return types.SimpleNamespace(**vars(_install(version)), kicad_cli=Path(f"cli-{version}"))
    found = [cli("10.99.0"), cli("10.0.5"), cli("9.0.4")]
    monkeypatch.setattr(kicad_env, "discover", lambda refresh=False: found)
    assert ks.choose_kicad_cli(ks.K9_SCH_FORMAT) == ("cli-9.0.4", "9.0.4")       # the oldest
    assert ks.choose_kicad_cli(20250610) == ("cli-10.99.0", "10.99.0")           # the newest
    monkeypatch.setattr(kicad_env, "discover", lambda refresh=False: found[2:])
    with pytest.raises(RuntimeError, match="nightly"):
        ks.choose_kicad_cli(20250610)
    monkeypatch.setattr(kicad_env, "discover", lambda refresh=False: [])
    with pytest.raises(RuntimeError, match="no kicad-cli"):
        ks.choose_kicad_cli(ks.K9_SCH_FORMAT)


def test_unextracted_footprints_point_at_the_projects_own_libraries(tmp_path: Path) -> None:
    (tmp_path / "parts.pretty").mkdir()
    (tmp_path / "fp-lib-table").write_text(
        '(fp_lib_table (version 7)\n'
        '  (lib (name "parts") (type "KiCad") (uri "${KIPRJMOD}/parts.pretty") (options "") '
        '(descr ""))\n'
        '  (lib (name "gone") (type "KiCad") (uri "${KIPRJMOD}/gone.pretty") (options "") '
        '(descr "")))\n', encoding="utf-8")
    board = kp.read_board(_mini_board('(footprint "parts:X" (layer "F.Cu") (at 1 1) '
                                      '(property "Reference" "U1"))',
                                      '(footprint "gone:Y" (layer "F.Cu") (at 2 2) '
                                      '(property "Reference" "U2"))'))
    assert kf.project_footprint_libs(tmp_path, board) == {
        "parts": str((tmp_path / "parts.pretty").resolve())}
    assert kf.project_footprint_libs(tmp_path / "parts.pretty", board) == {}    # no table
