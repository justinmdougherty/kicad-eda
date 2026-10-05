"""server/tools/dsn_sexpr.py: the Specctra DSN / SES s-expression reader (X-01).

Unit tier (no KiCad): the tokenizer (the declared string_quote character, the
lone quote token that declares it, quoted names with spaces and parentheses
such as "Net-(D1-A)", pin references joining quoted and bare parts, a quote
character inside a bare name as KiCad 9.0.4 writes it (5", K1-7"), a quoted
part that may not cross a line, ASCII-only whitespace), the span tree
(parse_nodes / find_all / child / walk) on the hand-written fixture
tests/fixtures/dsn/quoted_names.dsn.sexpr (a class net list KiCad wrapped
across 3 lines, "#RESET", a bare /ANT_2"), replace_spans (edits against the
original offsets, overlap and range errors, every other byte unchanged),
quote (KiCad's GetQuoteChar rule incl. a leading '#') / unquote /
declared_quote, the list tree (parse / emit / find / children: the
deterministic layout, the round trip and byte stability), DsnError on
malformed input, and the module's own constraints (stdlib only, no regexes,
Python 3.11 grammar, < 400 lines).

Live tier (skipped without a KiCad install whose bundled Python imports
pcbnew), under the OLDEST install's Python (KiCad 9.0: 3.11), which runs
swig_export_dsn.py: the module imports and round-trips the fixture; and
KiCad's own ExportSpecctraDSN of a board built in memory writes every probe
net name exactly as quote() does, and every name and pin reference reads
back through parse + unquote.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

import dsn_sexpr as ds

TOOLS = Path(__file__).resolve().parents[1] / "server" / "tools"
MODULE = TOOLS / "dsn_sexpr.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dsn" / "quoted_names.dsn.sexpr"

HEADER = ('(pcb x\n  (parser\n    (string_quote ")\n    (space_in_quoted_tokens on)\n'
          '    (host_cad "KiCad\'s Pcbnew")\n  )\n)')
WRAPPED_NETS = ['""', '"/LED (red)"', '"Net-(D1-A)"', '"a b"', '"unconnected-(U1-PA0-Pad13)"',
                '"unconnected-(U1-PA1-Pad12)"', '"unconnected-(U1-PA2-Pad11)"',
                '"unconnected-(U1-PA3-Pad10)"', '"unconnected-(U1-PA4-Pad9)"']


@pytest.fixture(scope="module")
def text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _kinds(tokens: list[ds.Token]) -> list[tuple[str, str]]:
    return [(t.kind, t.value) for t in tokens]


def _words(text: str) -> list[str]:
    return [t.value for t in ds.tokenize(text) if t.kind in ("atom", "string")]


# ----------------------------------------------------------------------------
# Tokenizer
# ----------------------------------------------------------------------------
def test_tokens_carry_kind_value_and_a_span_into_the_text() -> None:
    src = '(net "a b"\n  GND)'
    tokens = ds.tokenize(src)
    assert _kinds(tokens) == [("lpar", "("), ("atom", "net"), ("string", '"a b"'),
                              ("atom", "GND"), ("rpar", ")")]
    assert all(src[t.start:t.end] == t.value for t in tokens)
    assert (tokens[2].start, tokens[2].end) == (5, 10)


def test_the_quote_after_string_quote_is_a_lone_atom_not_a_string_opener() -> None:
    tokens = ds.tokenize(HEADER)
    k = [t.value for t in tokens].index("string_quote")
    assert _kinds(tokens[k - 1:k + 3]) == [("lpar", "("), ("atom", "string_quote"),
                                           ("atom", '"'), ("rpar", ")")]
    assert ("string", '"KiCad\'s Pcbnew"') in _kinds(tokens)
    assert ds.parse(HEADER)[2][1] == ["string_quote", '"']


def test_the_declared_quote_character_quotes_every_later_token() -> None:
    src = "(pcb x (parser (string_quote ')) (net 'Net-(D1-A)' (pins say\"hi R1-1)))"
    assert ds.declared_quote(src) == "'"
    assert ("string", "'Net-(D1-A)'") in _kinds(ds.tokenize(src))
    assert ("atom", 'say"hi') in _kinds(ds.tokenize(src))   # '"' is a plain character now
    assert ds.find(ds.parse(src), "net") == ["net", "'Net-(D1-A)'", ["pins", 'say"hi', "R1-1"]]


@pytest.mark.parametrize("src", ['(lpar string_quote "a b")', '(net string_quote "a b")',
                                 '("string_quote" "a b")'])
def test_only_a_list_headed_string_quote_declares(src: str) -> None:
    assert ds.declared_quote(src) == '"'
    assert ("string", '"a b"') in _kinds(ds.tokenize(src))


def test_the_default_quote_is_double_quote_when_nothing_declares_one() -> None:
    assert ds.declared_quote('(net "Net-(D1-A)")') == '"'
    assert ds.declared_quote(HEADER) == '"'
    assert _kinds(ds.tokenize('(net "Net-(D1-A)")'))[2] == ("string", '"Net-(D1-A)"')


@pytest.mark.parametrize("name", ['"Net-(D1-A)"', '"a b"', '"x (y) z"', '""', '"a\tb"',
                                  '"((("', '")"', '"#RESET"'])
def test_quoted_names_with_spaces_and_parentheses_are_one_string_token(name: str) -> None:
    src = f"(net {name} (pins R1-1))"
    assert _kinds(ds.tokenize(src))[2] == ("string", name)
    assert ds.parse(src) == ["net", name, ["pins", "R1-1"]]


def test_a_pin_reference_joining_quoted_and_bare_parts_stays_one_token() -> None:
    src = '(pins "R 1"-2 U1-"A-1" "J (1)"-"(2)" C1-1)'
    assert _kinds(ds.tokenize(src))[2:6] == [("atom", '"R 1"-2'), ("atom", 'U1-"A-1"'),
                                             ("atom", '"J (1)"-"(2)"'), ("atom", "C1-1")]
    assert ds.emit(ds.parse(src)) == src


# Verbatim KiCad 9.0.4 pcbnew.ExportSpecctraDSN output (CRLF, as on Windows) for nets named
# 5", a"b"c, #RESET, A#B, ... and pads 'X1'/'#4', '#P1'/'1', 'K1'/'7"', 'Q"1'/'3', 'J (1)'/'(2)',
# 'U1'/'A-1', 'R 1'/'2'. KiCad writes a name holding the quote character bare when nothing
# else forces quoting, and joins a pin reference as [q]comp[q]-[q]pin[q] (specctra.h PIN_REF).
KICAD9_NETWORK = (
    '(network\r\n'
    '    (net 5"\r\n      (pins F14-2 F14-1)\r\n    )\r\n'
    '    (net a"b"c\r\n      (pins F15-2 F15-1)\r\n    )\r\n'
    '    (net PINREFS\r\n'
    '      (pins X1-"#4" "#P1"-1 K1-7" Q"1-3 "J (1)"-"(2)" U1-"A-1" "R 1"-2)\r\n    )\r\n'
    '    (class kicad_default "#" "#2" "#RESET" \'q\' +3V3 -5V /CH1/OUT "/LED (red)"\r\n'
    '      5" "50%" A#B GND KiCad\'s "Net-(D1-A)" PINREFS "R1-1" "a b" a"b"c a\\b\r\n'
    '      x$y "x}" "{x}" Üñí\r\n'
    '      (circuit\r\n        (use_via "Via[0-1]_600:300_um")\r\n      )\r\n    )\r\n  )')
KICAD9_PINS = ['X1-#4', '#P1-1', 'K1-7"', 'Q"1-3', 'J (1)-(2)', 'U1-A-1', 'R 1-2']
KICAD9_CLASS = ["#", "#2", "#RESET", "'q'", "+3V3", "-5V", "/CH1/OUT", "/LED (red)", '5"', "50%",
                "A#B", "GND", "KiCad's", "Net-(D1-A)", "PINREFS", "R1-1", "a b", 'a"b"c', "a\\b",
                "x$y", "x}", "{x}", "Üñí"]


def test_a_quote_character_inside_a_bare_name_is_a_plain_character() -> None:
    tree = ds.parse(KICAD9_NETWORK)
    nets = ds.children(tree, "net")
    assert [n[1] for n in nets] == ['5"', 'a"b"c', "PINREFS"]
    assert [ds.unquote(p) for p in ds.find(nets[2], "pins")[1:]] == KICAD9_PINS
    names = [ds.unquote(x) for x in ds.find(tree, "class")[2:] if isinstance(x, str)]
    assert names == KICAD9_CLASS
    assert ds.emit(ds.parse(ds.emit(tree))) == ds.emit(tree)


def test_a_quote_opens_a_quoted_part_only_at_a_token_start_or_after_a_dash() -> None:
    src = '(pins 5" K1-7" Q"1-3 Q"1-"A 1" -"x y" "a"b" a-b"c)'
    assert _words(src)[1:] == ['5"', 'K1-7"', 'Q"1-3', 'Q"1-"A 1"', '-"x y"', '"a"b"', 'a-b"c']
    assert [t.kind for t in ds.tokenize(src)[2:-1]] == ["atom"] * 7


@pytest.mark.parametrize("src, offset", [
    ('(net "a\nb")', 5), ('(net "a\r\nb")', 5), ('(n x-"y\n  "z")', 5),
    ('(pcb\n (net "x\n (pins "y")))', 11)])
def test_a_quoted_part_ends_on_its_own_line(src: str, offset: int) -> None:
    """KiCad's DSNLEXER reads line by line ('Un-terminated delimited string'): a stray quote
    is an error, never a token that swallows the following lines."""
    with pytest.raises(ds.DsnError, match="unterminated") as err:
        ds.tokenize(src)
    assert err.value.offset == offset


def test_a_bare_hash_token_is_an_atom_not_a_comment() -> None:
    # KiCad never writes one (it quotes a leading '#'); this reader takes no comments
    assert ds.parse('(class c #X GND\n  "#Y" A#B)') == ["class", "c", "#X", "GND", '"#Y"', "A#B"]


def test_only_ascii_whitespace_separates_tokens() -> None:
    src = "(net A\u00a0B\u2003C\tD\r\nE\x0cF)"
    assert _words(src) == ["net", "A\u00a0B\u2003C", "D", "E", "F"]


def test_a_leading_byte_order_mark_is_skipped() -> None:
    assert ds.parse("\ufeff(pcb x)") == ["pcb", "x"]


# ----------------------------------------------------------------------------
# Malformed input -> DsnError (a ValueError) with the offset
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("reader", [ds.tokenize, ds.parse_nodes, ds.parse])
def test_an_unterminated_string_raises_with_its_offset(reader) -> None:
    with pytest.raises(ds.DsnError) as err:
        reader('(pcb x\n  (net "Net-(D1-A) (pins R1-1)))')
    assert isinstance(err.value, ValueError)
    assert err.value.offset == 14
    assert "unterminated" in str(err.value) and "line 2, column 8" in str(err.value)


@pytest.mark.parametrize("reader", [ds.parse_nodes, ds.parse])
@pytest.mark.parametrize("src, offset, words", [
    ("(pcb (a (b)", 5, "never closed"),
    ("(pcb (a))\n)", 10, "closes no list"),
    ("(pcb a))", 7, "closes no list"),
    ("pcb (a)", 0, "outside any list"),
    ("(pcb a) x", 8, "outside any list")])
def test_unbalanced_input_raises_with_the_offset(reader, src: str, offset: int,
                                                 words: str) -> None:
    with pytest.raises(ds.DsnError) as err:
        reader(src)
    assert err.value.offset == offset
    assert words in str(err.value)


@pytest.mark.parametrize("src", ["(parser (string_quote))", "(parser (string_quote (x)))",
                                 "(parser (string_quote"])
def test_a_string_quote_without_a_character_raises(src: str) -> None:
    with pytest.raises(ds.DsnError, match="string_quote"):
        ds.tokenize(src)


@pytest.mark.parametrize("src, count", [("", 0), ("  \n", 0), ("(a) (b)", 2)])
def test_parse_wants_exactly_one_top_level_list(src: str, count: int) -> None:
    assert len(ds.parse_nodes(src)) == count
    with pytest.raises(ds.DsnError, match=f"found {count}"):
        ds.parse(src)


# ----------------------------------------------------------------------------
# Span tree on the fixture
# ----------------------------------------------------------------------------
def test_a_class_net_list_wrapped_across_three_lines_parses_whole(text: str) -> None:
    (root,) = ds.parse_nodes(text)
    network = ds.child(root, "network")
    default, power = ds.find_all(network, "class")
    head = [t for t in default.items if isinstance(t, ds.Token)]
    assert [t.value for t in head] == ["kicad_default", *WRAPPED_NETS]
    assert [t.kind for t in head[1:]] == ["string"] * len(WRAPPED_NETS)
    assert text.count("\n", default.start, head[-1].end) == 2          # three lines
    assert [n.name for n in default.items if isinstance(n, ds.Node)] == ["circuit", "rule"]
    power_head = [t for t in power.items if isinstance(t, ds.Token)]
    assert [(t.kind, t.value) for t in power_head] == [
        ("atom", "Power"), ("string", '"#RESET"'), ("atom", "+3V3"), ("atom", '/ANT_2"'),
        ("atom", "GND")]
    assert [ds.unquote(t.value) for t in power_head[1:]] == ["#RESET", "+3V3", '/ANT_2"', "GND"]


def test_spans_index_the_original_text(text: str) -> None:
    (root,) = ds.parse_nodes(text)
    nodes = list(ds.walk(root))
    assert nodes[0] is root and len(nodes) > 100
    for node in nodes:
        assert text[node.start] == "(" and text[node.end - 1] == ")"
        assert ds.parse(text[node.start:node.end])[0] == node.name
        for item in node.items:
            assert node.start < item.start < item.end <= node.end
            if isinstance(item, ds.Token):
                assert text[item.start:item.end] == item.value


def test_find_all_and_child_look_at_direct_children_only(text: str) -> None:
    (root,) = ds.parse_nodes(text)
    structure = ds.child(root, "structure")
    assert [n.name for n in ds.find_all(structure, "layer")] == ["layer", "layer"]
    assert ds.find_all(root, "layer") == []
    assert ds.child(root, "nothing") is None
    keepout = ds.child(structure, "keepout")
    assert keepout.items[0] == ds.Token("string", '""', keepout.items[0].start,
                                        keepout.items[0].start + 2)
    assert ds.child(keepout, "polygon").name == "polygon"
    assert len([n for n in ds.walk(root) if n.name == "pins"]) == 12


def test_a_list_without_an_atom_head_has_an_empty_name() -> None:
    (root,) = ds.parse_nodes("(() ((a) b))")
    assert root.name == ""
    empty, inner = root.items
    assert (empty.name, empty.items) == ("", ())
    assert inner.name == "" and [type(x).__name__ for x in inner.items] == ["Node", "Token"]


def test_nodes_and_tokens_are_frozen(text: str) -> None:
    (root,) = ds.parse_nodes(text)
    with pytest.raises(AttributeError):
        root.name = "x"                                          # type: ignore[misc]
    with pytest.raises(AttributeError):
        ds.tokenize("(a)")[1].value = "b"                        # type: ignore[misc]
    assert isinstance(root.items, tuple)


# ----------------------------------------------------------------------------
# replace_spans
# ----------------------------------------------------------------------------
def test_a_span_edit_leaves_every_other_byte_identical(text: str) -> None:
    (root,) = ds.parse_nodes(text)
    path = ds.child(ds.child(ds.child(root, "structure"), "boundary"), "path")
    new = "(path pcb 0  129880 -80120  100120 -80120  100120 -99880  129880 -99880  129880 -80120)"
    out = ds.replace_spans(text, [(path.start, path.end, new)])
    assert out[:path.start] == text[:path.start]
    assert out[path.start:path.start + len(new)] == new
    assert out[path.start + len(new):] == text[path.end:]
    (again,) = ds.parse_nodes(out)
    for name in ("parser", "placement", "library", "network", "wiring"):
        before, after = ds.child(root, name), ds.child(again, name)
        assert out[after.start:after.end] == text[before.start:before.end]


def test_edits_apply_against_the_original_offsets_in_any_order() -> None:
    src = "(class a b c)"
    edits = [(9, 10, '"Net-(D1-A)"'), (7, 8, "B"), (12, 12, " d"), (1, 1, "")]
    assert ds.replace_spans(src, edits) == '(class B "Net-(D1-A)" c d)'
    assert ds.replace_spans(src, iter(edits)) == ds.replace_spans(src, reversed(edits))
    assert ds.replace_spans(src, []) == src
    assert ds.replace_spans(src, [(7, 7, "x"), (7, 7, "y"), (7, 8, "z")]) == "(class xyz b c)"


@pytest.mark.parametrize("edits", [[(2, 6, "x"), (5, 8, "y")], [(2, 6, "x"), (2, 6, "y")],
                                   [(2, 6, "x"), (4, 4, "y")], [(0, 13, ""), (12, 13, ")")]])
def test_overlapping_edits_raise(edits: list) -> None:
    with pytest.raises(ValueError, match="overlap"):
        ds.replace_spans("(class a b c)", edits)


@pytest.mark.parametrize("edit", [(-1, 2, "x"), (3, 2, "x"), (0, 14, "x"), (0, 1, 5),
                                  (0.0, 1, "x")])
def test_edits_outside_the_text_or_badly_typed_raise(edit: tuple) -> None:
    with pytest.raises((ValueError, TypeError)):
        ds.replace_spans("(class a b c)", [edit])


# ----------------------------------------------------------------------------
# quote / unquote
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("name, token", [
    ("GND", "GND"), ("+3V3", "+3V3"), ("-5V", "-5V"), ("/CH1/OUT", "/CH1/OUT"),
    ("kicad_eda_plane_GND", "kicad_eda_plane_GND"), ("Net-(D1-A)", '"Net-(D1-A)"'),
    ("a b", '"a b"'), ("", '""'), ("R1-1", '"R1-1"'), ("50%", '"50%"'), ("{x}", '"{x}"'),
    ("x}", '"x}"'), ("tab\there", '"tab\there"'), ("Üñí", "Üñí"), ("KiCad's", "KiCad's"),
    # KiCad quotes a LEADING '#' (richio.cpp GetQuoteChar: "so a symbol is not confused with a
    # comment"); Freerouting 1.9 reads a bare '#X' as a comment to the end of the line
    ("#RESET", '"#RESET"'), ("#2", '"#2"'), ("#", '"#"'), ("A#B", "A#B"),
    ("semi;colon", "semi;colon"), ("a\\b", "a\\b"), ("x$y", "x$y"), ("'q'", "'q'")])
def test_quote_follows_kicads_rule(name: str, token: str) -> None:
    assert ds.quote(name) == token
    (tok,) = [t for t in ds.tokenize(f"(n {token})") if t.kind in ("atom", "string")][1:]
    assert ds.unquote(tok.value) == name


def test_quote_uses_the_declared_quote_character() -> None:
    assert ds.quote("Net-(D1-A)", "'") == "'Net-(D1-A)'"
    assert ds.quote('say "hi"', "'") == "'say \"hi\"'"
    assert ds.quote("#RESET", "$") == "$#RESET$"
    assert ds.unquote("'Net-(D1-A)'", "'") == "Net-(D1-A)"


@pytest.mark.parametrize("name, qc", [('a"b', '"'), ('"', '"'), ('5"', '"'), ("it's", "'"),
                                      ("a\nb", '"'), ("a\rb", '"'), ("x", "("), ("x", " "),
                                      ("x", '""'), ("x", "")])
def test_quote_refuses_what_dsn_cannot_write(name: str, qc: str) -> None:
    with pytest.raises(ValueError):
        ds.quote(name, qc)


@pytest.mark.parametrize("token, name", [
    ('"R 1"-2', "R 1-2"), ('U1-"A-1"', "U1-A-1"), ("GND", "GND"), ('""', ""),
    ('"J (1)"-"(2)"', "J (1)-(2)"), ('X1-"#4"', "X1-#4"), ('"#P1"-1', "#P1-1"),
    ('5"', '5"'), ('a"b"c', 'a"b"c'), ('K1-7"', 'K1-7"'), ('Q"1-"A 1"', 'Q"1-A 1'),
    ('"a"b"', 'ab"')])
def test_unquote_drops_the_quotes_the_tokenizer_reads_as_quoting(token: str, name: str) -> None:
    assert ds.unquote(token) == name
    assert [t.value for t in ds.tokenize(f"(n {token})")][2] == token


def test_unquote_refuses_an_unterminated_quoted_part() -> None:
    with pytest.raises(ds.DsnError, match="unterminated"):
        ds.unquote('U1-"A')


# ----------------------------------------------------------------------------
# List tree: parse / emit / find / children
# ----------------------------------------------------------------------------
def test_parse_returns_nested_lists_of_token_text() -> None:
    src = '(pcb x\n  (net "a b" (pins A-1\n    B-2))(wiring))'
    assert ds.parse(src) == ["pcb", "x", ["net", '"a b"', ["pins", "A-1", "B-2"]], ["wiring"]]


EMIT_IN = ('(pcb x (parser (string_quote ") (host_cad "KiCad\'s Pcbnew")) (structure (layer F.Cu '
           '(type signal) (property (index 0))) (boundary (path pcb 0 1 2\n 3 4)))'
           '(network (class c "" "Net-(D1-A)" (circuit (use_via V)) tail) (net "a b" (pins A-1))))')
EMIT_OUT = """(pcb x
  (parser
    (string_quote ")
    (host_cad "KiCad's Pcbnew"))
  (structure
    (layer F.Cu
      (type signal)
      (property
        (index 0)))
    (boundary
      (path pcb 0 1 2 3 4)))
  (network
    (class c "" "Net-(D1-A)"
      (circuit
        (use_via V))
      tail)
    (net "a b"
      (pins A-1))))"""


def test_emit_layout_is_deterministic() -> None:
    assert ds.emit(ds.parse(EMIT_IN)) == EMIT_OUT
    assert ds.emit(["a", "b"]) == "(a b)"
    assert ds.emit([]) == "()"
    assert ds.emit([["a"], "b"]) == "(\n  (a)\n  b)"
    assert ds.emit("GND") == "GND"
    assert ds.emit(("a", ("b",))) == "(a\n  (b))"


@pytest.mark.parametrize("src", [EMIT_IN, HEADER, '(pins "R 1"-2 U1-"(A)")', "(a)", "(())"])
def test_round_trip_and_byte_stability(src: str) -> None:
    tree = ds.parse(src)
    once = ds.emit(tree)
    assert ds.parse(once) == tree
    assert ds.emit(ds.parse(once)) == once
    assert _words(once) == _words(src)


def test_round_trip_on_the_fixture_keeps_every_token(text: str) -> None:
    tree = ds.parse(text)
    once = ds.emit(tree)
    assert ds.parse(once) == tree
    assert ds.emit(ds.parse(once)) == once
    assert _words(once) == _words(text)
    assert ds.declared_quote(once) == '"'


@pytest.mark.parametrize("tree, exc", [(["a", ""], ValueError), (["a", 3], TypeError),
                                       (None, TypeError), (["a", {"b": 1}], TypeError)])
def test_emit_refuses_what_it_cannot_print(tree: object, exc: type) -> None:
    with pytest.raises(exc):
        ds.emit(tree)                                            # type: ignore[arg-type]


def test_find_and_children_look_at_direct_child_lists(text: str) -> None:
    tree = ds.parse(text)
    network = ds.find(tree, "network")
    assert [n[1] for n in ds.children(network, "class")] == ["kicad_default", "Power"]
    assert ds.find(network, "class")[2:2 + len(WRAPPED_NETS)] == WRAPPED_NETS
    assert [n[1] for n in ds.children(network, "net")][-2:] == ['"#RESET"', '/ANT_2"']
    assert len(ds.children(network, "net")) == 12
    assert ds.find(tree, "class") is None and ds.children(tree, "class") == []
    assert ds.find(ds.find(tree, "wiring"), "via")[:2] == ["via", '"Via[0-1]_600:300_um"']


def test_each_parse_returns_a_fresh_tree(text: str) -> None:
    first = ds.parse(text)
    first[0] = "changed"
    ds.find(first, "network").clear()
    second = ds.parse(text)
    assert second[0] == "pcb" and len(ds.find(second, "network")) > 1


# ----------------------------------------------------------------------------
# Module constraints: stdlib only, no regexes, Python 3.11 grammar, < 400 lines
# ----------------------------------------------------------------------------
def test_module_is_small_stdlib_only_and_regex_free() -> None:
    src = MODULE.read_text(encoding="utf-8")
    assert len(src.splitlines()) < 400
    tree = ast.parse(src, feature_version=(3, 11))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}
    assert "re" not in imported
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name) and node.value.id == "re"]


# ----------------------------------------------------------------------------
# Live tier: the oldest KiCad install's bundled Python (KiCad 9.0: 3.11)
# ----------------------------------------------------------------------------
_LIVE = """import sys
sys.path.insert(0, sys.argv[1])
import dsn_sexpr as ds
text = open(sys.argv[2], encoding="utf-8").read()
tree = ds.parse(text)
once = ds.emit(tree)
assert ds.parse(once) == tree and ds.emit(ds.parse(once)) == once
print(sys.version_info[:2], len(ds.tokenize(text)), len(list(ds.walk(ds.parse_nodes(text)[0]))))
"""


@pytest.fixture(scope="module")
def oldest_kicad_python() -> str:
    import kicad_env as env
    found = [i for i in env.discover() if i.python is not None]
    if not found:
        pytest.skip(f"no KiCad python with pcbnew: {env.probe_errors()}")
    return str(min(found, key=lambda i: i.version_tuple).python)


def _kicad_python(python: str, code: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([python, "-c", code, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180,
                          stdin=subprocess.DEVNULL)


def test_live_imports_and_round_trips_under_kicad_python(oldest_kicad_python: str) -> None:
    r = _kicad_python(oldest_kicad_python, _LIVE, str(TOOLS), str(FIXTURE))
    assert r.returncode == 0, r.stderr[-2000:]
    expected = ds.tokenize(FIXTURE.read_text(encoding="utf-8"))
    version, tokens, nodes = r.stdout.strip().rsplit("\n", 1)[-1].rsplit(" ", 2)
    assert int(tokens) == len(expected) and int(nodes) > 100
    assert version.startswith("(3, ")


# A board built in memory (one 2-pad SMD footprint per net, plus single-pad footprints whose
# pads all join net PINREFS), exported by KiCad's own pcbnew.ExportSpecctraDSN.
_EXPORT = """import json, sys
sys.path.insert(0, sys.argv[1])
import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew
names, pin_refs = json.loads(sys.argv[3])
mm, xy = pcbnew.FromMM, pcbnew.VECTOR2I
board = pcbnew.BOARD()
rect = pcbnew.PCB_SHAPE(board)
rect.SetShape(pcbnew.SHAPE_T_RECT)
rect.SetStart(xy(0, 0))
rect.SetEnd(xy(mm(60), mm(10 + 3 * (len(names) + len(pin_refs)))))
rect.SetLayer(pcbnew.Edge_Cuts)
rect.SetWidth(mm(0.1))
board.Add(rect)
def footprint(ref, numbers, net, row):
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference(ref)
    fp.SetPosition(xy(mm(10), mm(5 + 3 * row)))
    board.Add(fp)
    for k, number in enumerate(numbers):
        pad = pcbnew.PAD(fp)
        pad.SetNumber(number)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetShape(pcbnew.F_Cu, pcbnew.PAD_SHAPE_RECT)
        pad.SetSize(pcbnew.F_Cu, xy(mm(1), mm(1)))
        pad.SetLayerSet(pcbnew.PAD.SMDMask())
        pad.SetPosition(xy(mm(10 + 2 * k), mm(5 + 3 * row)))
        pad.SetNet(net)
        fp.Add(pad)
nets = []
for name in names + ["PINREFS"]:
    nets.append(pcbnew.NETINFO_ITEM(board, name))
    board.Add(nets[-1])
for row, net in enumerate(nets[:-1]):
    footprint("F%d" % row, ["1", "2"], net, row)
for row, (ref, number) in enumerate(pin_refs, start=len(names)):
    footprint(ref, [number], nets[-1], row)
print("exported", pcbnew.ExportSpecctraDSN(board, sys.argv[2]))
"""
LIVE_NAMES = ["GND", "+3V3", "-5V", "/CH1/OUT", "Net-(D1-A)", "a b", "R1-1", "50%", "{x}", "x}",
              "#RESET", "#2", "#", "A#B", '5"', 'a"b"c', "Üñí", "KiCad's",
              "unconnected-(U1-PA0-Pad13)", "/LED (red)", "tab\there", "x$y", "'q'"]
LIVE_PIN_REFS = [["R 1", "2"], ["U1", "A-1"], ["J (1)", "(2)"], ['Q"1', "3"], ["K1", '7"'],
                 ["#P1", "1"], ["X1", "#4"]]


def test_live_kicad_export_writes_every_name_as_quote_does(oldest_kicad_python: str,
                                                           tmp_path: Path) -> None:
    dsn = tmp_path / "names.dsn"
    r = _kicad_python(oldest_kicad_python, _EXPORT, str(TOOLS), str(dsn),
                      json.dumps([LIVE_NAMES, LIVE_PIN_REFS]))
    assert r.returncode == 0 and "exported True" in r.stdout, (r.stdout, r.stderr[-2000:])
    text = dsn.read_text(encoding="utf-8")
    network = ds.find(ds.parse(text), "network")
    written = {ds.unquote(n[1]): n for n in ds.children(network, "net")}
    assert set(written) == {*LIVE_NAMES, "PINREFS"}
    for name in LIVE_NAMES:          # KiCad writes a name holding the quote character bare
        assert written[name][1] == (name if '"' in name else ds.quote(name)), name
    pins = ds.find(written["PINREFS"], "pins")[1:]
    assert sorted(map(ds.unquote, pins)) == sorted(f"{c}-{p}" for c, p in LIVE_PIN_REFS)
    (cls,) = ds.children(network, "class")
    assert {ds.unquote(x) for x in cls[2:] if isinstance(x, str)} == set(written)
