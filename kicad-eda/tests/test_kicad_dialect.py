"""kicad_dialect: KiCad 10/11 footprint and symbol libraries in KiCad 9's
dialect (ROADMAP D0).

Unit tier: the scanner, every POLICY row, the KiCad 9 grammar whitelist, the
file helpers, k9_check / survey / CLI against a fake kicad-cli.

Live tier (each test skips cleanly when what it needs is missing):
* KiCad 9.x: the committed grammar is what build_grammar makes of its
  install; every K9_PROVEN construct survives a KiCad 9 re-save; the fixtures
  pass k9_check; its bundled Python imports the module (stdlib only).
* + the 10.99 nightly and $KICAD_EDA_FOREIGN_SAMPLES (CC-BY-SA, never
  committed: eagle/Qwiic_Button.{brd,sch}, adafruit/Adafruit Feather M0
  RFMxx.{brd,sch}): the nightly's Eagle imports downgrade completely and
  KiCad 9 loads them with identical pads and pins.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import textwrap
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

import kicad_dialect as kd

FIX = Path(__file__).resolve().parent / "fixtures" / "dialect"
FP_FIX = FIX / "Dialect_Test.kicad_mod"
SYM_FIX = FIX / "dialect_test.kicad_sym"
TOOLS = Path(kd.__file__).resolve().parent


def _text(path: Path) -> str:
    return kd._read(path)


def _labels(changes: tuple[kd.Change, ...]) -> set[str]:
    return {c.rule for c in changes}


def _fp(body: str, version: int = 20260901) -> str:
    return (f'(footprint "T"\n\t(version {version})\n\t(generator "pcbnew")\n'
            f'\t(generator_version "10.99")\n\t(layer "F.Cu")\n{body}\n)\n')


def _lib(body: str, version: int = 20260830) -> str:
    return (f'(kicad_symbol_lib\n\t(version {version})\n\t(generator "kicad_symbol_editor")\n'
            f'\t(generator_version "10.99")\n{body}\n)\n')


def _sym(name: str, body: str) -> str:
    return f'\t(symbol "{name}"\n\t\t(in_bom yes)\n{body}\n\t)'


# ----------------------------------------------------------------------------
# Scanner
# ----------------------------------------------------------------------------
def test_parse_builds_a_tree_with_offsets():
    text = '(a "x (y" 1 (b c) d)'
    root = kd.parse(text)
    assert (root.head, root.atoms, root.start, root.end) == ("a", ('"x (y"', "1", "d"), 0, 20)
    (b,) = root.kids
    assert (b.head, b.atoms, text[b.start:b.end]) == ("b", ("c",), "(b c)")
    assert root.kid("b") is b and root.kid("zz") is None and root.first == '"x (y"'


def test_parentheses_inside_strings_are_text():
    root = kd.parse('(a "(" ")" "\\"(" (b ")"))')
    assert root.atoms == ('"("', '")"', '"\\"("') and len(root.kids) == 1
    assert kd.parse('("lead" x)').head == "" and kd.parse("((b) c)").head == ""


@pytest.mark.parametrize("bad", ["(a", "a)", "(a))", '(a "x)', "x (a)", "(a) (b)", "", "  "])
def test_parse_refuses_malformed_text(bad):
    with pytest.raises(kd.DialectError):
        kd.parse(bad)


def test_parse_depth_and_bom():
    root = kd.parse("\ufeff(a (b (c d) e) (f))", depth=1)
    assert [k.head for k in root.kids] == ["b", "f"]
    assert root.kids[0].kids == () and root.kids[0].atoms == ("e",)
    with pytest.raises(kd.DialectError):
        kd.parse("(a (b (c", depth=1)


def test_unquote_and_keywords():
    assert kd.unquote('"a \\"b\\" \\\\ \\n"') == 'a "b" \\ \n' and kd.unquote("bare") == "bare"
    assert kd.parse('(x yes "s" -1.5 1e-05 .5 F.Cu)').keywords == ["yes", "F.Cu"]
    assert kd.parse("(data |abc def|)").keywords == []
    assert kd.parse("(x (version 12))").version == 12
    assert kd.parse("(x (version v))").version is None and kd.parse("(x)").version is None


# ----------------------------------------------------------------------------
# Footprints
# ----------------------------------------------------------------------------
def test_footprint_fixture_downgrades_in_place():
    src = _text(FP_FIX)
    res = kd.downgrade_footprint(src)
    assert res.ok and (res.from_version, res.to_version) == (20260901, kd.K9_FP_VERSION)
    assert res.names == ("Dialect_Test (v2)",)
    assert _labels(res.dropped) == {"(duplicate_pad_numbers_are_jumpers no)", "(point ...)",
                                    "(knockout no) in (fp_text_box)"}
    assert _labels(res.rewritten) == {"KICAD10_3DMODEL_DIR -> KICAD9_3DMODEL_DIR"}
    out = res.text
    assert "(version 20241229)" in out and '(generator_version "9.0")' in out
    assert "${KICAD9_3DMODEL_DIR}/Resistor_SMD.3dshapes" in out
    for gone in ("(point", "duplicate_pad_numbers_are_jumpers", "knockout", "20260901"):
        assert gone not in out
    for kept in ('"Keep (out"', 'a quoted ( paren and an escaped \\" quote"', "pad_prop_mechanical",
                 "(border no)", '"a note with ) and ( inside"'):
        assert kept in out
    point = '\n\t(point\n\t\t(at 0 0)\n\t\t(size 1.27)\n\t\t(layer "F.Fab")\n\t)'
    expected = src
    for old, new in (("(version 20260901)", "(version 20241229)"), ('"10.99"', '"9.0"'),
                     ("\n\t(duplicate_pad_numbers_are_jumpers no)", ""), (point, ""),
                     ("\n\t\t(knockout no)", ""), ("KICAD10_3DMODEL", "KICAD9_3DMODEL")):
        assert expected.count(old) == 1
        expected = expected.replace(old, new)
    assert out == expected                      # everything else byte for byte
    assert kd.rows("fp", out) == kd.rows("fp", src) and len(kd.rows("fp", src)) == 3


_CIRCLE_PAD = ('\t(pad "1" smd custom (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask")'
               ' (primitives (gr_circle (center 0 0) (end 1 0) (width 0.2){})))')


def test_pad_rows_take_an_absent_primitive_circle_fill_as_no():
    absent = kd.rows("fp", _fp(_CIRCLE_PAD.format("")))
    assert absent == kd.rows("fp", _fp(_CIRCLE_PAD.format(" (fill no)")))
    assert absent != kd.rows("fp", _fp(_CIRCLE_PAD.format(" (fill yes)")))


def test_downgrade_is_idempotent_and_leaves_old_dialects_alone():
    once = kd.downgrade_footprint(_text(FP_FIX)).text
    again = kd.downgrade_footprint(once)
    assert again.text == once and again.ok and not again.dropped and not again.rewritten
    assert (again.from_version, again.to_version) == (kd.K9_FP_VERSION, kd.K9_FP_VERSION)
    old = _fp("\t(frobnicate yes)", version=20221018)
    assert kd.downgrade_footprint(old).text == old
    bare = '(footprint "T" (layer "F.Cu"))'
    assert kd.downgrade_footprint(bare).text == bare
    lib = kd.downgrade_symbol_library(_text(SYM_FIX)).text
    assert kd.downgrade_symbol_library(lib).text == lib


SAMPLES = {   # a value for a row that matches any first atom
    "duplicate_pad_numbers_are_jumpers": "yes", "jumper_pad_groups": '("1" "2")',
    "point": '(at 0 0) (size 1) (layer "F.Fab")', "duplicate_pin_numbers_are_jumpers": "yes",
    "jumper_pin_groups": '("1" "2")', "body_styles": '"A" "B"', "radius": "0.5"}


def _policy_case(key: tuple[str, str, str, str]) -> tuple[str, str]:
    """(item text holding the construct of a POLICY row, the construct)."""
    kind, parent, head, value = key
    construct = f"({head} {value or SAMPLES[head]})"
    if kind == "fp":
        if parent == "footprint":
            return _fp(f"\t{construct}"), construct
        return _fp(f'\t(fp_text_box "x" (start 0 0) (end 1 1) (layer "F.Fab") {construct})'), \
            construct
    if parent == "symbol":
        return _lib(_sym("S", f"\t\t{construct}")), construct
    return _lib(_sym("S", f'\t\t(symbol "S_0_1" (rectangle (start 0 0) (end 1 1) {construct}))')), \
        construct


@pytest.mark.parametrize("key", list(kd.POLICY), ids=lambda k: "-".join(p for p in k if p))
def test_every_policy_row(key):
    rule = kd.POLICY[key]
    text, construct = _policy_case(key)
    res = (kd.downgrade_footprint if key[0] == "fp" else kd.downgrade_symbol_library)(text)
    if rule.action == kd.REFUSE:
        assert not res.ok
        message = res.refused or res.refused_items[0][2]
        construct_ = res.construct or res.refused_items[0][1]
        assert key[2] in construct_ and rule.note in message
        if key[0] == "fp":
            assert res.refused.startswith("footprint 'T': ") and res.text == ""
        else:
            assert res.refused_items[0][0] == "S" and '(symbol "S"' not in res.text
        return
    assert res.ok and construct not in res.text
    changes = res.dropped if rule.action == kd.DROP else res.rewritten
    (change,) = changes
    assert key[2] in change.rule and change.note == rule.note and change.count == 1
    if rule.action == kd.REWRITE:
        assert rule.replace in res.text and change.rule.endswith(f"-> {rule.replace}")


def test_unknown_lists_and_keywords_are_refused_with_name_and_item():
    res = kd.downgrade_footprint(_fp("\t(frobnicate 1)"))
    assert res.construct == "(frobnicate ...) in (footprint)" and res.text == ""
    assert res.refused == "footprint 'T': (frobnicate ...) in (footprint): not KiCad 9 grammar"
    res = kd.downgrade_footprint(_fp("\t(attr smd sparkly)"))
    assert res.refused == "footprint 'T': keyword sparkly in (attr): not KiCad 9 grammar"
    res = kd.downgrade_footprint('(footprint "T" (version 20260901) (pad "1" smd rect (warp 1)))')
    assert res.construct == "(warp ...) in (pad)"
    res = kd.downgrade_footprint(_fp('\t(pad "1" smd rect (property pad_prop_sparkly))'))
    assert res.construct == "keyword pad_prop_sparkly in (property)"


def test_grammar_takes_both_booleans_and_skips_embedded_data():
    assert "no" in kd.GRAMMAR["fp"]["border"] and "yes" in kd.GRAMMAR["fp"]["border"]
    body = ('\t(embedded_files (file (name "m.step") (type model) (data |KLUv/aB yes+/x|) '
            '(checksum "00")))')
    assert kd.downgrade_footprint(_fp(body)).ok
    assert kd.downgrade_footprint(_fp("\t(fp_text_box \"x\" (border no))")).ok


def test_unreadable_and_foreign_text_is_refused():
    res = kd.downgrade_footprint('(footprint "T" (version 20260901)')
    assert res.construct == "unreadable" and res.refused.startswith("unreadable: ")
    res = kd.downgrade_footprint(_lib(""))
    assert res.construct == "not a library" and "not a footprint file" in res.refused
    assert kd.downgrade_symbol_library(_fp("")).construct == "not a library"


# ----------------------------------------------------------------------------
# Symbol libraries
# ----------------------------------------------------------------------------
ACCEPTED = ["R", "R_Tiny", "R_Small", "GND", "VLOCAL", "NAND", "BOX"]   # R_Tiny follows R
MOVED = "(extends ...) after its parent"


def test_symbol_fixture_keeps_good_symbols_and_drops_refused_ones():
    src = _text(SYM_FIX)
    res = kd.downgrade_symbol_library(src)
    assert not res.ok and res.refused is None
    assert list(res.names) == ["R_Tiny", "R", *ACCEPTED[2:], "JUMPER", "JUMPER_ALT", "WEIRD"]
    assert {n: c for n, c, _ in res.refused_items} == {
        "JUMPER": "(duplicate_pin_numbers_are_jumpers yes)",
        "JUMPER_ALT": '(extends "JUMPER")', "WEIRD": "(frobnicate ...) in (symbol)"}
    assert _names(res.text) == ACCEPTED
    assert _labels(res.dropped) == {"(duplicate_pin_numbers_are_jumpers no)", "(in_pos_files yes)",
                                    "(in_pos_files no)", "(body_styles demorgan)",
                                    "(radius ...) in (rectangle)"}
    assert {c.rule: c.count for c in res.rewritten} == {
        "(power global) -> (power)": 1, "(power local) -> (power)": 1, MOVED: 1}
    notes = {c.rule: c.note for c in res.rewritten}
    assert "local power scope lost" in notes["(power local) -> (power)"]
    assert "follows its parent" in notes[MOVED]
    assert "(version 20241209)" in res.text and '"Resistor (generic) with \\"quotes\\"' in res.text
    assert "(show_name yes)" in res.text and "(do_not_autoplace yes)" in res.text
    tiny = _symbol_text(src, "R_Tiny").replace("\n\t\t(in_pos_files yes)", "")
    assert _symbol_text(res.text, "R") + "\n\t" + tiny + "\n\t" in res.text   # indent kept
    for name in ACCEPTED:
        assert _symbol_rows(src, name) == _symbol_rows(res.text, name)
    assert any("'NAND_1_2'" in row or "'1_2'" in row for row in _symbol_rows(src, "NAND"))


def _names(text: str) -> list[str]:
    return [kd.unquote(k.first) for k in kd.parse(text, depth=1).kids if k.head == "symbol"]


def _symbol_text(text: str, name: str) -> str:
    lib = kd.parse(text, depth=1)
    kid = next(k for k in lib.kids if k.head == "symbol" and kd.unquote(k.first) == name)
    return text[kid.start:kid.end]


def _symbol_rows(text: str, name: str) -> tuple[str, ...]:
    return kd.rows("sym", _symbol_text(text, name))


def test_moves_put_derived_symbols_after_their_parents():
    assert kd._moves(["A", "B"], {"A": "B", "B": None}) == {"B": ["A"]}
    assert kd._moves(["C", "B", "A"], {"C": "B", "B": "A"}) == {"A": ["B", "C"]}
    assert kd._moves(["A", "B"], {"B": "A"}) == {}                  # already in order
    assert kd._moves(["X", "Y", "Z"], {"X": "gone", "Y": "Z", "Z": "Y"}) == {}   # left for k9


def test_a_backwards_chain_is_reordered_and_a_refused_parent_takes_its_children():
    chain = [_sym("C", '\t\t(extends "B")'), _sym("B", '\t\t(extends "A")'), _sym("A", "")]
    res = kd.downgrade_symbol_library(_lib("\n".join(chain)))
    assert res.ok and res.names == ("C", "B", "A")
    assert res.text == _lib("\n".join(chain[::-1]), 20241209).replace('"10.99"', '"9.0"')
    assert {c.rule: c.count for c in res.rewritten} == {MOVED: 2}
    res = kd.downgrade_symbol_library(_lib("\n".join(
        [_sym("K", '\t\t(extends "J")'), _sym("J", "\t\t(frobnicate)"), _sym("L", "")])))
    assert [(n, c) for n, c, _ in res.refused_items] == [
        ("J", "(frobnicate ...) in (symbol)"), ("K", '(extends "J")')]
    assert _names(res.text) == ["L"] and not res.rewritten


def test_a_bad_library_level_item_refuses_the_whole_library():
    res = kd.downgrade_symbol_library(_lib("\t(frobnicate)\n" + _sym("S", "")))
    assert res.refused == "symbol library: (frobnicate ...) in (kicad_symbol_lib): " \
                          "not KiCad 9 grammar" and res.text == ""
    res = kd.downgrade_symbol_library(_lib("").replace("(kicad_symbol_lib", "(kicad_symbol_lib x"))
    assert res.construct == "keyword x in (kicad_symbol_lib)"


# ----------------------------------------------------------------------------
# Files
# ----------------------------------------------------------------------------
def _pretty(tmp_path: Path) -> Path:
    src = tmp_path / "src.pretty"
    src.mkdir()
    shutil.copy(FP_FIX, src / "Dialect_Test.kicad_mod")
    kd._write(src / "Jumpered.kicad_mod", _fp("\t(duplicate_pad_numbers_are_jumpers yes)"))
    kd._write(src / "Old.kicad_mod", _fp("", version=20241229))
    return src


def test_downgrade_pretty(tmp_path):
    src, dst = _pretty(tmp_path), tmp_path / "dst.pretty"
    dst.mkdir()
    kd._write(dst / "Jumpered.kicad_mod", "stale")
    rep = kd.downgrade_pretty(src, dst)
    assert not rep["ok"] and rep["counts"] == {"items": 3, "ok": 2, "refused": 1}
    assert sorted(p.name for p in dst.iterdir()) == ["Dialect_Test.kicad_mod", "Old.kicad_mod"]
    assert rep["items"]["Jumpered"] == {
        "ok": False, "construct": "(duplicate_pad_numbers_are_jumpers yes)",
        "reason": "footprint 'T': (duplicate_pad_numbers_are_jumpers yes): pads sharing a "
                  "number are " + kd._JUMPERS}
    assert rep["dropped"]["(point ...)"] == 1 and rep["items"]["Old"]["from_version"] == 20241229
    assert rep["items"]["Dialect_Test"]["notes"]["(point ...)"].startswith("a KiCad 10")
    assert (dst / "Dialect_Test.kicad_mod").read_bytes() == \
        kd.downgrade_footprint(_text(FP_FIX)).text.encode()
    part = kd.downgrade_pretty(src, tmp_path / "part.pretty", names=["Old", "Nope", "Old"])
    assert list(part["items"]) == ["Old", "Nope"]
    assert part["items"]["Nope"]["construct"] == "missing"
    with pytest.raises(ValueError):
        kd.downgrade_pretty(src, src)
    with pytest.raises(FileNotFoundError):
        kd.downgrade_pretty(tmp_path / "none.pretty", dst)


def test_downgrade_symbol_file(tmp_path):
    dst = tmp_path / "out" / "lib.kicad_sym"
    rep = kd.downgrade_symbol_file(SYM_FIX, dst)
    assert rep["written"] and not rep["ok"]
    assert rep["counts"] == {"items": 10, "ok": 7, "refused": 3}
    assert rep["items"]["R"] == {"ok": True}
    assert rep["items"]["WEIRD"]["reason"] == \
        "symbol 'WEIRD': (frobnicate ...) in (symbol): not KiCad 9 grammar"
    assert rep["rewritten"] == {"(power global) -> (power)": 1, "(power local) -> (power)": 1,
                                MOVED: 1}
    assert _text(dst) == kd.downgrade_symbol_library(_text(SYM_FIX)).text
    bad = tmp_path / "bad.kicad_sym"
    kd._write(bad, _lib("\t(frobnicate)"))
    rep = kd.downgrade_symbol_file(bad, dst)
    assert not rep["written"] and not dst.exists() and rep["items"]["(library)"]["construct"] == \
        "(frobnicate ...) in (kicad_symbol_lib)"
    with pytest.raises(ValueError):
        kd.downgrade_symbol_file(bad, bad)


# ----------------------------------------------------------------------------
# k9_check against a fake kicad-cli
# ----------------------------------------------------------------------------
class FakeCli:
    """`kicad-cli fp|sym upgrade --force -o OUT SRC` that fails a library with
    BADTOKEN in it and otherwise copies it, through `mutate`."""

    def __init__(self, mutate=lambda name, text: text, log: str = ""):
        self.mutate, self.log, self.calls = mutate, log, 0

    def __call__(self, argv: list[str], timeout_s: float) -> tuple[int, str]:
        self.calls += 1
        assert argv[1:5] == [argv[1], "upgrade", "--force", "-o"] and timeout_s > 0
        out, src = Path(argv[5]), Path(argv[6])
        texts = [p.read_text(encoding="utf-8") for p in (src.glob("*") if src.is_dir() else [src])]
        if any("BADTOKEN" in t for t in texts):
            return 2, "Unable to load library"
        if src.is_dir():
            out.mkdir()
            for p in src.glob("*.kicad_mod"):
                text = self.mutate(p.stem, p.read_text(encoding="utf-8"))
                if text is not None:
                    kd._write(out / p.name, text)
        else:
            kd._write(out, self.mutate(src.stem, src.read_text(encoding="utf-8")))
        return 0, self.log


@pytest.fixture
def down_pretty(tmp_path: Path) -> Path:
    dst = tmp_path / "down.pretty"
    kd.downgrade_pretty(_pretty(tmp_path), dst)
    return dst


def test_k9_check_ok_and_differing(monkeypatch, down_pretty):
    monkeypatch.setattr(kd, "_run", FakeCli())
    rep = kd.k9_check(down_pretty, "fp", "kicad-cli")
    assert rep["ok"] is True and (rep["items"], rep["loaded"], rep["compared"]) == (2, 2, 2)
    assert rep["differing"] == [] and rep["kicad_cli"] == "kicad-cli"
    monkeypatch.setattr(kd, "_run", FakeCli(lambda n, t: t.replace("(size 1 1.2)", "(size 1 1.3)")))
    rep = kd.k9_check(down_pretty, "fp", "kicad-cli")
    (diff,) = rep["differing"]
    assert rep["ok"] is False and diff["item"] == "Dialect_Test"
    assert "1.2" in diff["input"][0] and "1.3" in diff["k9"][0]
    monkeypatch.setattr(kd, "_run", FakeCli(lambda n, t: None if n == "Old" else t))
    rep = kd.k9_check(down_pretty, "fp", "kicad-cli")
    assert rep["ok"] is False and rep["missing"] == ["Old"]


def test_k9_check_bisects_a_library_kicad9_cannot_load(monkeypatch, down_pretty):
    for i in range(3):
        body = f'\t(descr "{"BADTOKEN" * (i == 1)}")'
        kd._write(down_pretty / f"F{i}.kicad_mod", _fp(body, 20241229))
    fake = FakeCli()
    monkeypatch.setattr(kd, "_run", fake)
    rep = kd.k9_check(down_pretty, "fp", "kicad-cli")
    assert rep["ok"] is False and rep["failed_items"] == ["F1"] and rep["missing"] == []
    assert (rep["items"], rep["compared"]) == (5, 4) and rep["differing"] == []
    assert any("exit 2: Unable to load library" in e for e in rep["errors"]) and fake.calls > 3
    rep = kd.k9_check(down_pretty, "fp", "kicad-cli", locate_failures=False)
    assert rep["failed_items"] == [] and len(rep["missing"]) == 5


def test_k9_check_symbols_bisect_with_their_parents(monkeypatch, tmp_path):
    lib = tmp_path / "s.kicad_sym"
    kd._write(lib, _lib("\n".join([_sym("A", '\t\t(property "Value" "BADTOKEN")'),
                                   _sym("B", '\t\t(extends "A")'), _sym("C", "")]), 20241209))
    monkeypatch.setattr(kd, "_run", FakeCli())
    rep = kd.k9_check(lib, "sym", "kicad-cli")
    assert rep["failed_items"] == ["A", "B"] and rep["compared"] == 1 and rep["ok"] is False


def test_k9_check_scans_for_error_lines_and_handles_no_kicad9(monkeypatch, down_pretty):
    monkeypatch.setattr(kd, "_run", FakeCli(log="12:00:01: Error: cannot read x\nfine\n"))
    rep = kd.k9_check(down_pretty / "Old.kicad_mod", "fp", "kicad-cli", locate_failures=False)
    assert rep["ok"] is False and rep["errors"][0] == "12:00:01: Error: cannot read x"
    monkeypatch.setattr(kd, "_run", lambda argv, t: (0, ""))
    rep = kd.k9_check(down_pretty, "fp", "kicad-cli", locate_failures=False)
    assert rep["errors"] == ["kicad-cli fp upgrade wrote nothing"]
    monkeypatch.setattr(kd, "_k9_install", lambda newest=False: None)
    rep = kd.k9_check(down_pretty, "fp")
    assert rep["ok"] is None and "no KiCad 9.x install" in rep["reason"]
    with pytest.raises(ValueError):
        kd.k9_check(down_pretty, "pcb", "kicad-cli")


def test_run_reports_launch_failures_and_timeouts():
    code, log = kd._run([str(Path(__file__).with_name("no-such-kicad-cli.exe"))], 5)
    assert code == -1 and "cannot start" in log
    code, log = kd._run([sys.executable, "-c", "import time; time.sleep(5)"], 0.3)
    assert code == -1 and "timed out" in log
    assert kd._run([sys.executable, "-c", "print('hi')"], 30) == (0, "hi\n")


class _Install:
    def __init__(self, version: tuple[int, int, int], root: Path):
        self.version_tuple, self.kicad_cli, self.share = version, root / "kicad-cli", root


def test_k9_install_picks_the_oldest_kicad9(monkeypatch, tmp_path):
    import kicad_env
    found = [_Install(v, tmp_path / str(v[0])) for v in ((10, 99, 0), (9, 0, 4), (9, 0, 1))]
    monkeypatch.setattr(kicad_env, "discover", lambda: found)
    assert kd._k9_install().version_tuple == (9, 0, 1)
    assert kd._k9_install(newest=True).version_tuple == (10, 99, 0)
    monkeypatch.setattr(kicad_env, "discover", lambda: [])
    assert kd._k9_install() is None and kd._k9_install(newest=True) is None


# ----------------------------------------------------------------------------
# Survey, grammar and CLI
# ----------------------------------------------------------------------------
def _share(tmp_path: Path) -> Path:
    share = tmp_path / "share"
    (share / "footprints").mkdir(parents=True)
    shutil.copytree(_pretty(tmp_path), share / "footprints" / "Lib.pretty")
    (share / "symbols").mkdir()
    shutil.copy(SYM_FIX, share / "symbols" / "syms.kicad_sym")
    kd._write(share / "symbols" / "bad.kicad_sym", _lib("\t(frobnicate)"))
    return share


def test_survey(monkeypatch, tmp_path):
    monkeypatch.setattr(kd, "_run", FakeCli())
    out = tmp_path / "rep" / "survey.json"
    rep = kd.survey(out, _share(tmp_path), jobs=2, kicad_cli="kicad-cli")
    fp, sym = rep["footprints"], rep["symbols"]
    assert (fp["items"], fp["refused"], fp["loaded"], fp["identical"]) == (3, 1, 2, 2)
    assert fp["refusals"]["(duplicate_pad_numbers_are_jumpers yes)"]["examples"] == ["Lib:Jumpered"]
    assert (sym["libraries"], sym["items"], sym["refused"], sym["identical"]) == (2, 11, 4, 7)
    assert sym["refusals"]['(extends "JUMPER")']["count"] == 1
    assert sym["percent_identical"] == 63.636 and sym["rewritten"][MOVED] == 1
    assert json.loads(out.read_text(encoding="utf-8"))["footprints"] == fp
    monkeypatch.setattr(kd, "_k9_install", lambda newest=False: None)
    with pytest.raises(RuntimeError):
        kd.survey(out, _share(tmp_path / "again"))


K9_SCH_VERSION = 20250114           # KiCad 9's schematic dialect: its demo lib_symbols
GRAMMAR_WIDTH = 99


def _pairs(node: kd.Node, parent: str, out: dict[str, set[str]]) -> None:
    out.setdefault(parent, set()).add("(" + node.head)
    out.setdefault(node.head, set()).update(node.keywords)
    for k in node.kids:
        _pairs(k, node.head, out)


def _k9_items(path: Path) -> list[tuple[str, kd.Node]]:
    """(parent, item) of a file in KiCad 9's own dialect: a library itself, a
    board's footprints and zones (footprint zones share their parser), a
    schematic's library symbols; [] for any other dialect."""
    text = kd._read(path)
    top = kd.parse(text, depth=1)
    want = {"footprint": kd.K9_FP_VERSION, "kicad_pcb": kd.K9_FP_VERSION,
            "kicad_symbol_lib": kd.K9_SYM_VERSION, "kicad_sch": K9_SCH_VERSION}.get(top.head)
    if want is None or top.version != want:
        return []
    if top.head in kd.ROOT_HEAD.values():
        return [(kd.ROOT, kd.parse(text))]
    if top.head == "kicad_pcb":
        return [(kd.ROOT if k.head == "footprint" else "footprint", kd.parse(text, k.start, k.end))
                for k in top.kids if k.head in ("footprint", "zone")]
    libs = [kd.parse(text, k.start, k.end) for k in top.kids if k.head == "lib_symbols"]
    return [("kicad_symbol_lib", s) for lib in libs for s in lib.kids if s.head == "symbol"]


def build_grammar(share: Path) -> dict[str, dict[str, frozenset[str]]]:
    """{"fp"|"sym": {parent: tokens}} of a KiCad 9 share/kicad (stock and demo
    files); a token is ``(head`` for a list, else a keyword."""
    globs = {"fp": ("footprints/*.pretty/*.kicad_mod", "demos/**/*.kicad_mod",
                    "demos/**/*.kicad_pcb"),
             "sym": ("symbols/*.kicad_sym", "demos/**/*.kicad_sym", "demos/**/*.kicad_sch")}
    out: dict[str, dict[str, frozenset[str]]] = {}
    for kind, patterns in globs.items():
        acc: dict[str, set[str]] = {}
        for path in (p for g in patterns for p in sorted(share.glob(g))):
            try:
                items = _k9_items(path)
            except (kd.DialectError, UnicodeDecodeError):
                continue
            for parent, node in items:
                _pairs(node, parent, acc)
        out[kind] = {p: frozenset(t) for p, t in acc.items() if t}
    return out


def format_grammar(grammar: Mapping[str, Mapping[str, Iterable[str]]]) -> str:
    """The _K9_GRAMMAR text: ``kind parent: tokens`` entries joined by ';' (an
    entry's label never wraps away from its first token)."""
    entries = ["\xa0".join([kind, parent + ":", *sorted(toks)[:1]]) + "".join(
        " " + t for t in sorted(toks)[1:]) for kind in sorted(grammar)
        for parent, toks in sorted(grammar[kind].items())]
    return textwrap.fill("; ".join(entries), GRAMMAR_WIDTH, break_long_words=False,
                         break_on_hyphens=False).replace("\xa0", " ")


def _k9_share(tmp_path: Path) -> Path:
    share = tmp_path / "k9"
    (share / "footprints" / "L.pretty").mkdir(parents=True)
    (share / "symbols").mkdir()
    (share / "demos" / "p").mkdir(parents=True)
    kd._write(share / "footprints" / "L.pretty" / "a.kicad_mod",
              '(footprint "a" (version 20241229) (attr smd) (pad "1" smd rect (at 0 0)))')
    kd._write(share / "footprints" / "L.pretty" / "old.kicad_mod",
              '(footprint "old" (version 20221018) (oldtoken))')
    kd._write(share / "footprints" / "L.pretty" / "broken.kicad_mod", '(footprint "b" (version')
    kd._write(share / "demos" / "p" / "p.kicad_pcb",
              '(kicad_pcb (version 20241229) (footprint "x" (pad "2" thru_hole circle))'
              ' (zone (fill yes (island_removal_mode 1))) (segment (start 0 0)))')
    kd._write(share / "symbols" / "s.kicad_sym",
              '(kicad_symbol_lib (version 20241209) (symbol "s" (pin input line)))')
    kd._write(share / "demos" / "p" / "p.kicad_sch",
              '(kicad_sch (version 20250114) (lib_symbols (symbol "t" (power))) (wire))')
    return share


def test_build_format_and_load_grammar(tmp_path):
    g = build_grammar(_k9_share(tmp_path))
    assert g["fp"] == {"/": {"(footprint"}, "footprint": {"(attr", "(pad", "(version", "(zone"},
                       "attr": {"smd"}, "pad": {"(at", "circle", "rect", "smd", "thru_hole"},
                       "zone": {"(fill"}, "fill": {"(island_removal_mode", "yes"}}
    assert g["sym"] == {"/": {"(kicad_symbol_lib"}, "kicad_symbol_lib": {"(symbol", "(version"},
                        "symbol": {"(pin", "(power"}, "pin": {"input", "line"}}
    text = format_grammar(g)
    assert "fp fill: (island_removal_mode yes;" in " ".join(text.split())
    assert all(len(line) <= GRAMMAR_WIDTH for line in text.splitlines())
    back = kd.load_grammar(text)
    assert back["fp"]["fill"] == {"(island_removal_mode", "yes", "no"}
    assert back == {"fp": {**g["fp"], "fill": back["fp"]["fill"]}, "sym": g["sym"]}


def test_committed_grammar_is_in_the_format_and_holds_the_proven_pairs():
    assert format_grammar(kd.load_grammar(kd._K9_GRAMMAR)).count(";") == \
        kd._K9_GRAMMAR.count(";")
    assert kd.load_grammar(format_grammar(kd.load_grammar(kd._K9_GRAMMAR))) == \
        kd.load_grammar(kd._K9_GRAMMAR)
    for kind, table in kd.load_grammar(kd.K9_PROVEN).items():
        for parent, toks in table.items():
            assert toks <= kd.GRAMMAR[kind][parent]
            assert not toks <= kd.load_grammar(kd._K9_GRAMMAR)[kind].get(parent, frozenset())


def test_cli(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(kd, "_run", FakeCli())
    good = tmp_path / "good.pretty"
    good.mkdir()
    shutil.copy(FP_FIX, good)
    assert kd.main(["fp", str(good), str(tmp_path / "g9.pretty"), "--check"]) == 0
    assert json.loads(capsys.readouterr().out)["k9_check"]["ok"] is True
    assert kd.main(["fp", str(_pretty(tmp_path)), str(tmp_path / "a9.pretty")]) == 1
    assert json.loads(capsys.readouterr().out)["counts"]["refused"] == 1
    assert kd.main(["sym", str(SYM_FIX), str(tmp_path / "s9.kicad_sym"), "--check"]) == 1
    check = json.loads(capsys.readouterr().out)["k9_check"]
    assert check["ok"] is True and check["compared"] == len(ACCEPTED)
    monkeypatch.setattr(kd, "_k9_install", lambda newest=False: _Install((9, 0, 4), tmp_path))
    assert kd.main(["survey", str(tmp_path / "s.json"), "--root", str(_share(tmp_path / "x"))]) == 0
    assert "libraries" not in json.loads(capsys.readouterr().out)
    with pytest.raises(SystemExit):
        kd.main(["grammar"])


# ----------------------------------------------------------------------------
# Live tier
# ----------------------------------------------------------------------------
NIGHTLY_CLI = Path(r"C:\Program Files\KiCad\10.99\bin\kicad-cli.exe")
SAMPLES_ENV = "KICAD_EDA_FOREIGN_SAMPLES"


@pytest.fixture(scope="module")
def kicad9():
    import kicad_env
    nines = [i for i in kicad_env.discover() if i.version_tuple[0] == 9 and i.share]
    if not nines:
        pytest.skip("no KiCad 9.x install")
    return min(nines, key=lambda i: i.version_tuple)


@pytest.fixture(scope="module")
def nightly():
    import kicad_env
    found = [i.kicad_cli for i in kicad_env.discover() if i.version_tuple >= (10, 99, 0)]
    if not found and not NIGHTLY_CLI.is_file():
        pytest.skip("no KiCad 10.99 nightly (its kicad-cli imports Eagle schematics)")
    return Path(found[0]) if found else NIGHTLY_CLI


def test_live_committed_grammar_is_what_kicad9_writes(kicad9):
    assert format_grammar(build_grammar(Path(kicad9.share))) == kd._K9_GRAMMAR.strip()


_FIELD = '\t\t(property {} (at 0 0 0) {} (effects (font (size 1 1))))'
PROVEN = {   # K9_PROVEN construct -> (KiCad 11 item, the re-save must hold / lack, field)
    "pad_prop_mechanical": ("fp", _fp('\t(pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.5)'
                                      ' (property pad_prop_mechanical) (layers "*.Cu"))'),
                            ["(property pad_prop_mechanical)"], [], None),
    "property hide": ("sym", _lib(_sym("S", _FIELD.format('"Value" "S"', "(hide yes)"))),
                      ["(hide yes)"], [], '"Value"'),
    "property private": ("sym", _lib(_sym("S", _FIELD.format('private "KLC" "x"', ""))),
                         ['(property private "KLC"'], [], None),
    "unit_name": ("sym", _lib(_sym("S", '\t\t(symbol "S_1_1" (unit_name "NMOS") (pin input line'
                                        ' (at 0 0 0) (length 2.54) (name "A") (number "1")))')),
                  ['(unit_name "NMOS")', '(number "1"'], [], None),
    "show_name/do_not_autoplace": (
        "sym", _lib(_sym("S", _FIELD.format('"Value" "S"', "(show_name yes) (do_not_autoplace yes)")
                         + "\n" + _FIELD.format('"Reference" "U"',
                                                "(show_name no) (do_not_autoplace no)"))),
        ["(show_name)", "(do_not_autoplace)"], ["show_name yes", "show_name no", "autoplace no"],
        None),
}


def _field_text(text: str, name: str) -> str:
    """The text of the top-level symbol's property `name` (quoted)."""
    sym = kd.parse(text).kid("symbol")
    prop = next(p for p in sym.kids if p.head == "property" and p.first == name)
    return text[prop.start:prop.end]


@pytest.mark.parametrize("name", list(PROVEN))
def test_k9_proven_survives_a_kicad9_resave(kicad9, tmp_path, name):
    kind, text, holds, lacks, field = PROVEN[name]
    res = (kd.downgrade_footprint if kind == "fp" else kd.downgrade_symbol_library)(text)
    assert res.ok and res.from_version > res.to_version, res.refused
    src = tmp_path / ("in.pretty" if kind == "fp" else "in.kicad_sym")
    if kind == "fp":
        src.mkdir()
        kd._write(src / "T.kicad_mod", res.text)
    else:
        kd._write(src, res.text)
    out = tmp_path / ("k9.pretty" if kind == "fp" else "k9.kicad_sym")
    code, log = kd._run([str(kicad9.kicad_cli), kind, "upgrade", "--force", "-o", str(out),
                         str(src)], 120)
    assert code == 0 and not kd._ERROR.search(log), log
    raw = _text(out / "T.kicad_mod" if kind == "fp" else out)
    saved = " ".join((_field_text(raw, field) if field else raw).split())
    assert all(h in saved for h in holds) and not any(x in saved for x in lacks), saved


def _primitive(text: str) -> str:
    pad = next(p for p in kd.parse(text).kids if p.head == "pad")
    return " ".join(text[pad.kid("primitives").start:pad.kid("primitives").end].split())


def test_live_pad_defaults_are_read_alike(kicad9, nightly, tmp_path):
    """An absent primitive circle fill: the nightly reads its own dialect's text and
    KiCad 9 the downgraded text as (fill no); each writes that on a re-save."""
    assert dict(kd.PAD_DEFAULTS) == {("gr_circle", "fill"): "(fill no)"}   # one proof each
    k11 = _fp(_CIRCLE_PAD.format(""))
    runs = ((nightly, k11), (kicad9.kicad_cli, kd.downgrade_footprint(k11).text))
    for i, (cli, text) in enumerate(runs):
        src, out = tmp_path / f"in{i}.pretty", tmp_path / f"out{i}.pretty"
        src.mkdir()
        kd._write(src / "T.kicad_mod", text)
        code, log = kd._run([str(cli), "fp", "upgrade", "--force", "-o", str(out), str(src)], 120)
        assert code == 0 and not kd._ERROR.search(log), log
        assert _primitive(_text(out / "T.kicad_mod")).endswith("(width 0.2) (fill no) ) )")


def test_live_fixtures_pass_k9_check(kicad9, tmp_path):
    good = tmp_path / "good.pretty"
    good.mkdir()
    shutil.copy(FP_FIX, good)
    rep = kd.downgrade_pretty(good, tmp_path / "fp9.pretty")
    assert rep["ok"]
    check = kd.k9_check(tmp_path / "fp9.pretty", "fp")
    assert check["ok"] is True and check["compared"] == 1, check
    kd.downgrade_symbol_file(SYM_FIX, tmp_path / "sym9.kicad_sym")
    check = kd.k9_check(tmp_path / "sym9.kicad_sym", "sym")
    assert check["ok"] is True and check["compared"] == len(ACCEPTED), check


def test_live_kicad9_python_imports_the_module(kicad9):
    python = kicad9.python or Path(kicad9.kicad_cli).with_name(
        "python.exe" if os.name == "nt" else "python3")
    if not Path(python).is_file():
        pytest.skip("KiCad 9 has no bundled Python here")
    code, log = kd._run([str(python), "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                         "import kicad_dialect; print(sys.version.split()[0])", str(TOOLS)], 120)
    assert code == 0, log
    assert log.strip().startswith("3.11")


EAGLE = {   # sample: (board, schematic, footprints, symbols) - counts the nightly imports
    "Qwiic_Button": ("eagle/Qwiic_Button.brd", "eagle/Qwiic_Button.sch", 33, 145),
    "Feather_M0_RFM": ("adafruit/Adafruit Feather M0 RFMxx.brd",
                       "adafruit/Adafruit Feather M0 RFMxx.sch", 24, 109),
}


@pytest.mark.parametrize("sample", list(EAGLE))
def test_live_eagle_import_downgrades_and_loads_in_kicad9(kicad9, nightly, tmp_path, sample):
    root = os.environ.get(SAMPLES_ENV)
    brd, sch, n_fp, n_sym = EAGLE[sample]
    if not root or not (Path(root) / brd).is_file() or not (Path(root) / sch).is_file():
        pytest.skip(f"${SAMPLES_ENV} does not hold {brd} and {sch}")
    shutil.copy(Path(root) / brd, tmp_path / f"{sample}.brd")
    shutil.copy(Path(root) / sch, tmp_path / f"{sample}.sch")
    for what, src in (("pcb", f"{sample}.brd"), ("sch", f"{sample}.sch")):
        out = tmp_path / f"k11.kicad_{what}"
        code, log = kd._run([str(nightly), what, "import", "--format", "eagle", "-o", str(out),
                             str(tmp_path / src)], 600)
        assert code == 0 and out.is_file() and not kd._ERROR.search(log), log[-800:]
    (pretty,) = tmp_path.glob("*.pretty")
    (symlib,) = tmp_path.glob("*.kicad_sym")
    fps = kd.downgrade_pretty(pretty, tmp_path / "k9.pretty")
    syms = kd.downgrade_symbol_file(symlib, tmp_path / "k9.kicad_sym")
    assert fps["ok"] and fps["counts"]["items"] == n_fp, fps["counts"]
    assert syms["ok"] and syms["counts"]["items"] == n_sym, syms["counts"]
    for kind, path, n in (("fp", tmp_path / "k9.pretty", n_fp),
                          ("sym", tmp_path / "k9.kicad_sym", n_sym)):
        check = kd.k9_check(path, kind)
        assert check["ok"] is True and check["differing"] == [], check
        assert (check["loaded"], check["compared"]) == (n, n), check
