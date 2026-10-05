"""server/tools/sexpr_read.py: the KiCad S-expression text readers (F46-01).

Unit tier (no KiCad): the public readers that net_equivalence and
kicad_import_pcb / _sch / _checks share (token and header regexes, parse,
atom / tag / children / child / value, the pad number and net, the board net
table, natural ordering, the teardrop repair and quote_bare_keys), the old
private names net_equivalence keeps for this wave, and the ``.kicad_pcb`` text
edge cases read through net_equivalence (pcb_pad_nets, moved here from
test_net_equivalence.py; board_duplicate_refs and load_pad_nets_meta on a
board), including KiCad 4/5 bare tokens: ``(pad 01 ...)`` is pad "01", never 1.

Live tier (skipped without a KiCad python that imports pcbnew): sexpr_read
imports under KiCad's bundled Python (KiCad 9: 3.11, stdlib only), and pcbnew
reads the bare-token KiCad 5 board exactly as pcb_pad_nets does.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import kicad_import_pcb as kp
import net_equivalence as ne
import sexpr_read as sr
from fixtures.kicad_install import KICAD

FIX = Path(__file__).resolve().parent / "fixtures" / "equiv"
TOOLS = Path(__file__).resolve().parents[1] / "server" / "tools"


# ----------------------------------------------------------------------------
# The names net_equivalence keeps for this wave (kicad_import.py: F46-11)
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("old, new", [
    ("_LEADING", "LEADING"), ("_TOKEN", "TOKEN_RE"), ("_HEAD", "HEAD_RE"),
    ("_FP_NAME", "FP_NAME_RE"), ("_VERSION", "VERSION_RE"), ("_head_of", "head_of"),
    ("_sexp", "parse"), ("_atom", "atom"), ("_tag", "tag"), ("_children", "children"),
    ("_child", "child"), ("_value", "value"), ("_natural", "natural_key"),
    ("_restore_teardrop_parens", "restore_teardrop_parens"), ("_pad_order", "pad_order"),
    ("_pad_number", "pad_number"), ("_pad_net", "pad_net"), ("_net_table", "net_table")])
def test_net_equivalence_keeps_the_old_private_names(old: str, new: str) -> None:
    assert getattr(ne, old) is getattr(sr, new)


def test_net_equivalence_re_exports_no_net_and_unescape() -> None:
    assert ne.NO_NET == sr.NO_NET == ""
    assert ne.unescape_net_name is sr.unescape_net_name
    assert sr.unescape_net_name("/A{slash}B") == "/A/B"


# ----------------------------------------------------------------------------
# Regexes, head_of, parse
# ----------------------------------------------------------------------------
def test_head_of_skips_a_bom_and_whitespace() -> None:
    assert sr.head_of("﻿ \r\n\t(kicad_pcb (version 1))") == "kicad_pcb"
    assert sr.head_of("( pad 1)") == "pad"
    assert sr.head_of('("quoted")') == sr.head_of("") == sr.head_of("x (a)") == ""


def test_token_and_header_regexes() -> None:
    text = '(footprint "X:Y (odd)" (property "Reference" "a ) b"))'
    assert [m.group() for m in sr.TOKEN_RE.finditer(text)] == [
        "(", '"X:Y (odd)"', "(", '"Reference"', '"a ) b"', ")", ")"]
    assert sr.FP_NAME_RE.match(text).group(1) == '"X:Y (odd)"'
    assert sr.FP_NAME_RE.match("(module R_0603 (layer F.Cu))").group(1) == "R_0603"
    assert sr.VERSION_RE.search("(kicad_pcb (version 20241229)").group(1) == "20241229"
    assert sr.HEAD_RE.match("(  net 1)").group(1) == "net"


def test_parse_reads_a_list_after_leading_whitespace() -> None:
    node = sr.parse('﻿\r\n (net 7 "SDA")')
    assert [sr.atom(x) for x in node] == ["net", "7", "SDA"] and node[1] == 7


def test_parse_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="cannot parse S-expression"):
        sr.parse('(a "unterminated)')
    with pytest.raises(ValueError, match="not an S-expression list"):
        sr.parse("42")                                   # sexpdata yields a bare number
    monkeypatch.setitem(sys.modules, "sexpdata", None)   # sexpdata not installed
    with pytest.raises(RuntimeError, match=r"needs sexpdata \(server/requirements\.txt\)"):
        sr.parse("(a)")


# ----------------------------------------------------------------------------
# atom / tag / children / child / value
# ----------------------------------------------------------------------------
def test_tree_helpers() -> None:
    node = sr.parse('(pad "1" smd (at 0 0) (net 3 "GND") (net 4 "X") (uuid "u") (empty))')
    assert sr.tag(node) == "pad" and sr.tag(["x"]) == "x"
    assert sr.tag([]) == sr.tag([["a"]]) == sr.tag("pad") == ""
    assert [sr.atom(n[2]) for n in sr.children(node, "net")] == ["GND", "X"]
    assert sr.children(None, "net") == [] and sr.child(None, "net") is None
    assert sr.child(node, "net")[1] == 3 and sr.child(node, "missing") is None
    assert sr.value(node, "uuid") == "u"
    assert sr.value(node, "empty") == sr.value(node, "missing") == ""
    assert sr.atom(7) == "7" and sr.atom(sr.parse("(a b)")[1]) == "b"
    with pytest.raises(ValueError, match="expected a value, found a list"):
        sr.atom(["a"])


# ----------------------------------------------------------------------------
# Pads, nets, the net table
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("src, number", [
    ('(pad "1" smd rect (layers "F.Cu" "F.Mask"))', "1"),
    ('(pad "7" np_thru_hole circle (layers "*.Cu"))', ""),     # KiCad clears NPTH numbers
    ('(pad "49" smd rect (layers "F.Paste"))', ""),            # an aperture: no copper layer
    ('(pad "50" smd rect (layers "B.Paste" "B.Mask"))', ""),
    ('(pad "A1" smd rect (layers "Dessus" "F.Mask"))', "A1"),  # KiCad 5 custom copper name
    ('(pad "3" smd rect)', "3"),
    ("(pad)", ""),                                             # no number token at all
    ("(pad (at 0 0))", ""),
])
def test_pad_number_is_the_number_kicad_keeps(src: str, number: str) -> None:
    assert sr.pad_number(sr.parse(src)) == number


@pytest.mark.parametrize("src, net", [
    ('(pad "1" (net 7 "SDA"))', "SDA"), ('(pad "1" (net "A{slash}B"))', "A/B"),
    ('(pad "1" (net 7))', "SDA"), ('(pad "1" (net 0))', ""), ('(pad "1" (net))', ""),
    ('(pad "1" (net ""))', ""), ('(pad "1" smd)', ""),
])
def test_pad_net_reads_every_kicad_form(src: str, net: str) -> None:
    assert sr.pad_net(sr.parse(src), {7: "SDA"}, "R1 pad 1") == net


def test_pad_net_fails_loudly_on_an_undefined_net_number() -> None:
    with pytest.raises(ValueError, match="R1 pad 1 references net 9"):
        sr.pad_net(sr.parse('(pad "1" (net 9))'), {}, "R1 pad 1")


def test_net_table_reads_numbered_nets_and_unescapes() -> None:
    sources = ['(net 0 "")', '(net 1 "/A{slash}B")', "(net 2 VCC)", "(net 3 01)", '(net "x")']
    assert sr.net_table(sources) == {0: "", 1: "/A/B", 2: "VCC", 3: "01"}


def test_natural_key_and_pad_order() -> None:
    assert sorted(["R10", "R2", "", "C1"], key=sr.natural_key) == ["", "C1", "R2", "R10"]
    assert sorted([("U1", "10"), ("U1", "2"), ("R1", "1")], key=sr.pad_order) == [
        ("R1", "1"), ("U1", "2"), ("U1", "10")]


def test_restore_teardrop_parens_only_outside_strings() -> None:
    clean = '(pad "1" (teardrops (curved_edges no) (filter_ratio 0.9)))'
    assert sr.restore_teardrop_parens(clean) is clean
    bad = '(pad "a) filter_ratio" (teardrops (curved_edges no)filter_ratio 0.9))'
    assert sr.restore_teardrop_parens(bad) == (
        '(pad "a) filter_ratio" (teardrops (curved_edges no)(filter_ratio 0.9))')


# ----------------------------------------------------------------------------
# KiCad 4/5 bare tokens: KiCad keeps their text, sexpdata would not
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("src, expected", [
    ("(pad 01 smd rect)", '(pad "01" smd rect)'),
    ("(pad 1e1 smd)", '(pad "1e1" smd)'),
    ("(pad 1_0 smd)", '(pad "1_0" smd)'),
    ('(pad "01" smd)', '(pad "01" smd)'),
    ("(fp_text reference 007 (at 0 0))", '(fp_text reference "007" (at 0 0))'),
    ("(module 0603 (layer F.Cu))", '(module "0603" (layer F.Cu))'),
    ("(net 3 1e1)", '(net 3 "1e1")'),
    ("(net 3) (pad_to_mask_clearance 0)", "(net 3) (pad_to_mask_clearance 0)"),
    ('(gr_text "(pad 01 x)" (pad 02))', '(gr_text "(pad 01 x)" (pad "02"))'),
    ("(pad a\\b smd)", '(pad "a\\\\b" smd)'),
])
def test_quote_bare_keys_keeps_the_token_text(src: str, expected: str) -> None:
    assert sr.quote_bare_keys(src) == expected


def test_quoted_bare_tokens_parse_to_their_text() -> None:
    for token in ("01", "1e1", "1_0", "+3", "-0", ".5", "a\\b"):
        assert sr.pad_number(sr.parse(sr.quote_bare_keys(f"(pad {token} smd)"))) == token


# board_k5.sexpr with KiCad 5 bare tokens that are not plain integers
_K5_EDITS = (("(pad 1 thru_hole rect", "(pad 01 thru_hole rect"),        # J1 pad 01
             ("(pad 2 thru_hole oval", "(pad 02 thru_hole oval"),        # J1 pad 02
             ("(pad 4 smd rect (at 0.95 0.65)", "(pad 1e1 smd rect (at 0.95 0.65)"),
             ("(pad 3 smd rect (at -0.95 0.65)", "(pad 1_0 smd rect (at -0.95 0.65)"),
             ("(fp_text reference H1 ", "(fp_text reference 007 "))
# pcbnew 9.0.4 ground truth for that board (see the live test)
K5_BARE = {
    "VCC": {("R1", "1"), ("C1", "1")},
    "Net-(R1-Pad2)": {("R1", "2"), ("R2", "1"), ("U1", "1")},
    "GND": {("R2", "2"), ("C1", "2"), ("U1", "EP"), ("J1", "02"), ("007", "1")},
    "unconnected-(U1-NC-Pad2)": {("U1", "2")},
    "/A/B": {("U1", "1e1"), ("J1", "01")},
    ne.NO_NET: {("U1", "1_0")},
}


def _k5_bare() -> str:
    text = (FIX / "board_k5.sexpr").read_text(encoding="utf-8")
    for old, new in _K5_EDITS:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text


def test_kicad5_bare_pad_numbers_and_references_keep_their_text() -> None:
    text = _k5_bare()
    assert ne.pcb_pad_nets(text) == K5_BARE
    board = kp.read_board(text)
    pads = {(fp.ref, p.number, p.net) for fp in board.footprints for p in fp.pads}
    assert pads == {(r, n, net) for net, keys in K5_BARE.items() for r, n in keys}
    assert [fp.ref for fp in board.footprints][-1] == "007"


# ----------------------------------------------------------------------------
# .kicad_pcb text edge cases, read through net_equivalence.pcb_pad_nets
# ----------------------------------------------------------------------------
def test_pcb_reader_resolves_a_number_only_pad_net_through_the_table() -> None:
    text = """(kicad_pcb (version 20241229) (generator "pcbnew")
      (net 0 "") (net 7 "SDA")
      (footprint "R" (layer "F.Cu") (property "Reference" "R1")
        (pad "1" smd rect (net 7)) (pad "2" smd rect (net 0)))
      (footprint "R" (layer "F.Cu") (property "Reference" "R2")
        (pad "1" smd rect (net 7 "SDA"))))"""
    assert ne.pcb_pad_nets(text) == {"SDA": {("R1", "1"), ("R2", "1")}, ne.NO_NET: {("R1", "2")}}


def test_pcb_reader_fails_loudly_on_an_undefined_net_number() -> None:
    text = """(kicad_pcb (version 20241229) (net 0 "")
      (footprint "R" (property "Reference" "R1") (pad "1" smd rect (net 9))))"""
    with pytest.raises(ValueError, match=r"R1 pad 1 .*net 9.*not define"):
        ne.pcb_pad_nets(text)


def test_pcb_reader_keeps_same_number_pads_on_different_nets_in_both() -> None:
    text = """(kicad_pcb (version 20260901)
      (footprint "U" (property "Reference" "U1")
        (pad "25" smd rect (net "GND")) (pad "25" smd rect (net "VCC"))))"""
    assert ne.pcb_pad_nets(text) == {"GND": {("U1", "25")}, "VCC": {("U1", "25")}}


def test_pcb_reader_ignores_parentheses_inside_quoted_strings() -> None:
    text = """(kicad_pcb (version 20241229)
      (gr_text "a ) tricky ( \\" text" (at 0 0))
      (footprint "X:Y (odd)" (property "Reference" "J(1)")
        (property "Value" ")))")
        (pad "1" smd rect (net 1 "Net-(J(1)-Pad1)"))))"""
    assert ne.pcb_pad_nets(text) == {"Net-(J(1)-Pad1)": {("J(1)", "1")}}


def test_pcb_reader_reads_empty_and_net_zero_pad_nets_as_no_net() -> None:
    text = """(kicad_pcb (version 20260901)
      (footprint "R" (property "Reference" "R1")
        (pad "1" smd rect (net)) (pad "2" smd rect (net 0)) (pad "3" smd rect (net ""))))"""
    assert ne.pcb_pad_nets(text) == {ne.NO_NET: {("R1", "1"), ("R1", "2"), ("R1", "3")}}


@pytest.mark.parametrize("reader, text, match", [
    (ne.pcb_pad_nets, '(kicad_pcb (version 1) (footprint "R" (pad "1" smd "unterminated)))',
     "unbalanced|cannot parse"),
    (ne.sexpr_netlist_pad_nets, '(export (version "E") (components))', r"no \(nets"),
    (ne.sexpr_netlist_pad_nets, '(export (nets (net (code "1") (name "A") (node (pin "1")))))',
     "node without ref"),
])
def test_s_expression_readers_fail_loudly(reader, text: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        reader(text)


def test_pcb_reader_drops_npth_and_aperture_pad_numbers_like_kicad() -> None:
    # PAD::CanHaveNumber: KiCad clears the number of NPTH pads and of pads on
    # no copper layer (paste-only apertures) when it loads a board
    text = """(kicad_pcb (version 20231120) (net 0 "") (net 1 "GND")
      (footprint "QFN" (property "Reference" "U5")
        (pad "49" smd rect (layers "F.Cu" "F.Mask") (net 1 "GND"))
        (pad "49" smd rect (layers "F.Paste"))
        (pad "50" smd rect (layers "B.Paste" "B.Mask")))
      (footprint "Hole" (fp_text reference "H1")
        (pad "1" np_thru_hole circle (layers *.Cu *.Mask)))
      (module "Old" (fp_text reference J1)
        (pad 1 connect rect (layers Dessus F.Mask) (net 1 GND))))"""
    assert ne.pcb_pad_nets(text) == {"GND": {("U5", "49"), ("J1", "1")}}


def test_pcb_reader_keeps_footprints_with_an_empty_reference_as_kicad_does() -> None:
    text = """(kicad_pcb (version 20240922)
      (footprint "" (property "Reference" "")
        (pad "1" smd rect (net "A")) (pad "2" smd rect (net "B")))
      (footprint "X" (property "Reference" "R1") (pad "1" smd rect (net "A"))))"""
    board = ne.pcb_pad_nets(text)
    assert board == {"A": {("", "1"), ("R1", "1")}, "B": {("", "2")}}
    assert ne.compare(board, board).equal


def test_pcb_reader_reads_teardrops_missing_a_paren_like_kicad_does() -> None:
    # KiCad 9.0.4's RoyalBlue54L-Feather demo has 349 of these; KiCad's
    # teardrop parser makes '(' optional, so the board loads in KiCad
    pad = """(pad "{n}" smd roundrect (net 1 "{net}")
          (teardrops (best_length_ratio 0.5) (max_width 2)
            (curved_edges no)filter_ratio 0.9)
            (enabled yes) (allow_two_segments yes)
          )
          (uuid "954fd78b-8651-4ccc-942c-16ae54494df9"))"""
    text = f"""(kicad_pcb (version 20241229) (net 0 "") (net 1 "a) filter_ratio")
      (footprint "C" (property "Reference" "C1") {pad.format(n=1, net="a) filter_ratio")}
        {pad.format(n=2, net="a) filter_ratio")})
      (footprint "C" (property "Reference" "C2") {pad.format(n=1, net="a) filter_ratio")}))"""
    assert ne.pcb_pad_nets(text) == {"a) filter_ratio": {("C1", "1"), ("C1", "2"), ("C2", "1")}}


@pytest.mark.parametrize("text, match", [
    ('(export (version "E"))', "not a KiCad board"),
    ("", "not a KiCad board"),
    ('(kicad_pcb (version 20241229) (footprint "R" (pad "1" smd rect)', "unbalanced"),
    ('(kicad_pcb (version 20241229)))', "unbalanced"),
])
def test_pcb_reader_rejects_what_is_not_a_board(text: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        ne.pcb_pad_nets(text)


# the microwave demo's three POLY and tiny_tapeout's STITCH1 vias, in small
_DUPLICATES = """(kicad_pcb (version 20241229) (net 0 "") (net 1 "GND")
  (footprint "MW" (property "Reference" "POLY") (pad "1" smd rect (net 1 "GND")))
  (footprint "MW" (property "Reference" "POLY") (pad "1" smd rect (net 1 "GND")))
  (footprint "MW" (property "Reference" "POLY") (pad "1" smd rect (net 0 "")))
  (footprint "V" (property "Reference" "STITCH1") (attr board_only) (pad "1" smd (net 1 "GND")))
  (footprint "V" (property "Reference" "STITCH1") (attr board_only) (pad "1" smd (net 1 "GND")))
  (footprint "R" (property "Reference" "R10") (pad "1" smd rect (net 1 "GND"))))"""


def test_duplicate_references_are_reported() -> None:
    assert ne.board_duplicate_refs(_DUPLICATES) == ["POLY", "STITCH1"]
    assert ne.board_duplicate_refs((FIX / "board_k9.sexpr").read_text(encoding="utf-8")) == []


def test_load_pad_nets_meta_describes_a_board(tmp_path: Path) -> None:
    board = tmp_path / "dup.kicad_pcb"
    board.write_text(_DUPLICATES, encoding="utf-8")
    nets, meta = ne.load_pad_nets_meta(board)
    assert meta == {"kind": "board", "format_version": 20241229,
                    "duplicate_refs": ["POLY", "STITCH1"], "board_only_refs": ["STITCH1"]}
    assert nets == ne.load_pad_nets(board) == ne.pcb_pad_nets(_DUPLICATES)
    nets, meta = ne.load_pad_nets_meta(board, exclude_board_only=True)
    assert nets == {"GND": {("POLY", "1"), ("R10", "1")}, ne.NO_NET: {("POLY", "1")}}
    assert meta["board_only_refs"] == ["STITCH1"]


# ----------------------------------------------------------------------------
# Live tier: KiCad's own Python
# ----------------------------------------------------------------------------
_PCBNEW_PADS = """import sys
sys.path.insert(0, sys.argv[1])
import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import json
import pcbnew
board = pcbnew.LoadBoard(sys.argv[2])
assert board is not None, "LoadBoard returned None"
print(json.dumps(sorted([fp.GetReference(), p.GetNumber(), p.GetNetname()]
                        for fp in board.GetFootprints() for p in fp.Pads())))
"""


@pytest.fixture(scope="session")
def kicad_python() -> Path:
    """The KiCad install's Python, verified to import pcbnew (probed on first
    use, never at collection)."""
    if KICAD is None or KICAD.cli is None:
        pytest.skip("kicad-cli not installed")
    import kicad_env as env
    python = env.bundled_python(KICAD.cli)
    if python is None:
        pytest.skip(f"no KiCad python with pcbnew: {env.python_probe_error(KICAD.cli)}")
    return python


def _run(python: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(python), *argv], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120,
                          stdin=subprocess.DEVNULL)


def test_live_sexpr_read_imports_under_kicad_python(kicad_python: Path) -> None:
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import sexpr_read as sr; "
            "print(sr.head_of(sr.quote_bare_keys('(kicad_pcb (pad 01))')), "
            "sr.natural_key('R10'), sys.version_info[:2])")
    r = _run(kicad_python, "-c", code, str(TOOLS))
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.startswith("kicad_pcb ('R', 10, '')")


def test_live_pcbnew_reads_bare_kicad5_tokens_like_pcb_pad_nets(kicad_python: Path,
                                                                tmp_path: Path) -> None:
    board = tmp_path / "k5_bare.kicad_pcb"
    board.write_text(_k5_bare(), encoding="utf-8")
    r = _run(kicad_python, "-c", _PCBNEW_PADS, str(TOOLS), str(board))
    assert r.returncode == 0, r.stderr[-2000:]
    got: dict[str, set[tuple[str, str]]] = {}
    for ref, number, net in json.loads(r.stdout.strip().splitlines()[-1]):
        if number:                                  # the NPTH pad has no number in KiCad
            got.setdefault(net, set()).add((ref, number))
    assert got == K5_BARE
