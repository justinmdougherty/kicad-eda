#!/usr/bin/env python3
"""Specctra DSN / SES s-expression text: tokenizer, span tree, list tree and a
deterministic printer (X-01).

The one DSN reader of the plugin: F3's outline rewrite (F3-02 / F3-17), F5's
canonical DSN order (F5-02) and D7's layer policy (D7-19) all go through it.
Never apply regular expressions to DSN structure: a regex stops at the first
'(' inside a quoted net name such as "Net-(D1-A)" and corrupts the file (T10
review). Stdlib only and Python 3.11 compatible: swig_export_dsn.py imports it
under KiCad 9's bundled Python.

KiCad DSN facts it honours:

- The header declares the quote character, ``(parser (string_quote ") ...)``.
  The lone ``"`` after ``string_quote`` IS that declaration (an atom), not a
  string opener; the declared character quotes every later token (default
  ``"``). DSN has no escapes.
- Quoted tokens may hold spaces and parentheses ("Net-(D1-A)", "a b");
  ``""`` is the empty name (``(keepout "" ...)``). A quoted part ends on its
  own line, as in KiCad's DSNLEXER (it reads line by line), so a stray quote
  raises DsnError instead of swallowing the lines after it.
- A token may join quoted and bare parts with no space between them: KiCad
  writes a pin reference as ``[q]comp[q]-[q]pin[q]`` (``"R 1"-2``,
  ``U1-"A-1"``). It stays ONE token, so printing never inserts a space.
- A quote character opens a quoted part only at a token's start or right
  after a bare '-'. Anywhere else it is a plain character: KiCad 9.0.4 writes
  a name holding the quote character bare when nothing else forces quoting
  (``(net 5"``, ``a"b"c``, ``K1-7"``, ``Q"1-3``). A bare token that STARTS
  with it (a name "x, a pin id "7) is ambiguous; KiCad's reader and
  Freerouting take it as a string opener too, and so does this one.
- KiCad quotes every name starting with '#' (``"#RESET"``): Freerouting 1.9
  reads a bare ``#...`` token as a comment to the end of the line. This
  reader takes no comments: a bare '#' token is an atom.
- Lists wrap across lines (class net lists, long paths). Only ASCII space,
  tab, CR, LF, FF and VT separate tokens: KiCad writes UTF-8 names bare.
  KiCad 9 on Windows writes CRLF; spans index whatever text was passed, so
  in-place rewrites read and write with ``newline=''``.
- Units are um (``(resolution um 10) (unit um)``).

Two APIs; callers pick one:

- Spans, for in-place edits that leave every other byte as it was:
  ``tokenize`` -> [Token(kind, value, start, end)], ``parse_nodes`` ->
  [Node(name, items, start, end)] (spans index the original text; ``items``
  are the Tokens and Nodes after the head atom), ``find_all`` / ``child`` /
  ``walk``, ``replace_spans`` and, to write names, ``quote`` / ``unquote`` /
  ``declared_quote``.
- Trees, for rewrites that re-print the whole file: ``parse`` -> nested lists
  of str (quoted tokens keep their quotes, so printing is lossless), ``emit``
  (deterministic layout: the atoms of a leaf list on one line, child lists one
  per line indented 2 spaces per level; no trailing newline, add one when
  writing a file), ``find`` / ``children``. parse(emit(parse(t))) == parse(t)
  and emit is byte-stable.

Malformed input (a quoted part with no closing quote on its line, unbalanced
parentheses, a token outside any list, a ``string_quote`` without a
character) raises DsnError, a ValueError carrying the offset.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Union

DEFAULT_QUOTE = '"'
WHITESPACE = frozenset(" \t\r\n\f\v")
_PARENS = frozenset("()")
_DELIMITERS = WHITESPACE | _PARENS
_LINE_BREAKS = ("\n", "\r")
_BOM = "\ufeff"
# what makes KiCad quote a name (richio.cpp OUTPUTFORMATTER::GetQuoteChar:
# "\t ()%{}", a leading '#', a '-' after the first character, or the empty
# name), plus the other separators this reader splits on; a line break
# cannot be written at all
_NEEDS_QUOTE = (WHITESPACE - frozenset(_LINE_BREAKS)) | _PARENS | frozenset("%{}")
_DECLARE = "string_quote"

LPAR, RPAR, ATOM, STRING = "lpar", "rpar", "atom", "string"


class DsnError(ValueError):
    """Malformed DSN text; ``offset`` indexes the text that was read."""

    def __init__(self, message: str, offset: int, text: str | None = None) -> None:
        where = f"offset {offset}"
        if text is not None:
            line = text.count("\n", 0, offset) + 1
            column = offset - (text.rfind("\n", 0, offset) + 1) + 1
            where += f" (line {line}, column {column})"
        super().__init__(f"{message} at {where}")
        self.offset = offset


@dataclass(frozen=True, slots=True)
class Token:
    kind: str       # 'lpar' | 'rpar' | 'atom' | 'string'
    value: str      # the token exactly as written (quotes included): text[start:end]
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Node:
    name: str                           # the head atom; '' for '()' or a list-headed list
    items: tuple[Node | Token, ...]     # everything after the head, in order
    start: int                          # offset of '('
    end: int                            # offset just after ')'


Tree = Union[str, list]


# ----------------------------------------------------------------------------
# Tokenizer
# ----------------------------------------------------------------------------
def _opens(text: str, i: int, start: int, quote: str) -> bool:
    """Whether ``text[i]`` opens a quoted part of the token that starts at
    ``start``: a quote character at the token's start or right after a bare
    '-' (KiCad's [q]comp[q]-[q]pin[q] pin reference). Anywhere else it is a
    plain character, as KiCad writes it in a bare name (5", K1-7", Q"1-3)."""
    return text[i] == quote and (i == start or text[i - 1] == "-")


def _close(text: str, i: int, quote: str) -> int:
    """The offset of the quote closing the part opened at ``i``. It must be on
    the same line, as KiCad's DSNLEXER requires ('Un-terminated delimited
    string'): a stray quote then fails here instead of swallowing the lines
    after it."""
    close = text.find(quote, i + 1)
    if close < 0 or any(text.find(eol, i + 1, close) >= 0 for eol in _LINE_BREAKS):
        raise DsnError("unterminated quoted string (no closing quote on its line)", i, text)
    return close


def _word(text: str, start: int, quote: str) -> Token:
    """The atom or string starting at ``start``: a run of non-separators in
    which each quote character that ``_opens`` a part runs to its ``_close``."""
    i, n = start, len(text)
    while i < n and text[i] not in _DELIMITERS:
        i = _close(text, i, quote) + 1 if _opens(text, i, start, quote) else i + 1
    value = text[start:i]
    whole = value[0] == quote and value.find(quote, 1) == len(value) - 1
    return Token(STRING if whole else ATOM, value, start, i)


def _declared(text: str, i: int) -> Token:
    """The one-character quote declaration after ``(string_quote``."""
    if i >= len(text) or text[i] in _PARENS:
        raise DsnError(f"{_DECLARE} declares no quote character", i, text)
    return Token(ATOM, text[i], i, i + 1)


def _scan(text: str) -> Iterator[Token]:
    quote = DEFAULT_QUOTE
    i = 1 if text.startswith(_BOM) else 0
    n = len(text)
    declaring = after_lpar = False
    while i < n:
        c = text[i]
        if c in WHITESPACE:
            i += 1
            continue
        if declaring:
            tok = _declared(text, i)
            quote = tok.value
        elif c in _PARENS:
            tok = Token(LPAR if c == "(" else RPAR, c, i, i + 1)
        else:
            tok = _word(text, i, quote)
        declaring = after_lpar and tok.kind == ATOM and tok.value == _DECLARE
        after_lpar = tok.kind == LPAR
        i = tok.end
        yield tok
    if declaring:
        _declared(text, n)


def tokenize(text: str) -> list[Token]:
    """Every token of ``text`` in order (see the module doc for the rules)."""
    return list(_scan(text))


def declared_quote(text: str) -> str:
    """The quote character ``text`` declares first with ``(string_quote X)``,
    else the default ``"``."""
    declaring = after_lpar = False
    for tok in _scan(text):                 # the same state _scan keeps
        if declaring:
            return tok.value
        declaring = after_lpar and tok.kind == ATOM and tok.value == _DECLARE
        after_lpar = tok.kind == LPAR
    return DEFAULT_QUOTE


# ----------------------------------------------------------------------------
# Span tree
# ----------------------------------------------------------------------------
def _node(items: list[Node | Token], start: int, end: int) -> Node:
    if items and isinstance(items[0], Token):
        return Node(items[0].value, tuple(items[1:]), start, end)
    return Node("", tuple(items), start, end)


def _build(text: str, leaf: Callable[[Token], Any],
           branch: Callable[[list, int, int], Any]) -> list:
    """The top-level lists of ``text``: ``leaf`` makes each atom or string,
    ``branch(items, start, end)`` each list."""
    roots: list = []
    stack: list[tuple[int, list]] = []
    for tok in _scan(text):
        if tok.kind == LPAR:
            stack.append((tok.start, []))
        elif tok.kind == RPAR:
            if not stack:
                raise DsnError("')' closes no list", tok.start, text)
            start, items = stack.pop()
            (stack[-1][1] if stack else roots).append(branch(items, start, tok.end))
        elif stack:
            stack[-1][1].append(leaf(tok))
        else:
            raise DsnError(f"token {tok.value[:40]!r} outside any list", tok.start, text)
    if stack:
        raise DsnError("'(' is never closed", stack[-1][0], text)
    return roots


def parse_nodes(text: str) -> list[Node]:
    """The top-level lists of ``text`` as span-preserving Nodes."""
    return _build(text, lambda tok: tok, _node)


def find_all(node: Node, name: str) -> list[Node]:
    """The direct child lists of ``node`` named ``name``, in order."""
    return [x for x in node.items if isinstance(x, Node) and x.name == name]


def child(node: Node, name: str) -> Node | None:
    """The first direct child list of ``node`` named ``name``, or None."""
    return next((x for x in node.items if isinstance(x, Node) and x.name == name), None)


def walk(node: Node) -> Iterator[Node]:
    """``node`` and every list inside it, depth first in text order."""
    pending = [node]
    while pending:
        current = pending.pop()
        yield current
        pending.extend(x for x in reversed(current.items) if isinstance(x, Node))


def _checked_edit(edit: Sequence, size: int) -> tuple[int, int, str]:
    start, end, new = edit
    if not (isinstance(start, int) and isinstance(end, int) and isinstance(new, str)):
        raise TypeError(f"an edit is (start: int, end: int, new_text: str), got {edit!r}")
    if not 0 <= start <= end <= size:
        raise ValueError(f"edit [{start}, {end}) is outside the text [0, {size})")
    return start, end, new


def replace_spans(text: str, edits: Iterable[Sequence]) -> str:
    """``text`` with each ``(start, end, new_text)`` applied, right to left in
    effect: every offset indexes the ORIGINAL text, so the edits may come in
    any order. Insertions at one offset keep their given order. Overlapping
    edits raise ValueError; nothing outside the edited spans changes."""
    ordered = sorted((_checked_edit(e, len(text)) for e in edits), key=lambda e: e[:2])
    pieces: list[str] = []
    prev = (0, 0)
    for start, end, new in ordered:
        if start < prev[1]:
            raise ValueError(f"edits overlap: [{prev[0]}, {prev[1]}) and [{start}, {end})")
        pieces += [text[prev[1]:start], new]
        prev = (start, end)
    pieces.append(text[prev[1]:])
    return "".join(pieces)


# ----------------------------------------------------------------------------
# Writing names
# ----------------------------------------------------------------------------
def _check_quote_char(quote_char: str) -> None:
    if len(quote_char) != 1 or quote_char in _DELIMITERS:
        raise ValueError(f"a quote character is one non-separator character, "
                         f"got {quote_char!r}")


def quote(name: str, quote_char: str = DEFAULT_QUOTE) -> str:
    """``name`` as a DSN token, quoted when KiCad quotes it (it is empty,
    starts with '#', holds a space, tab, '(', ')', '%', '{' or '}', or a '-'
    after its first character) and also when it holds a form feed or vertical
    tab. Refused with ValueError: a name holding the quote character (KiCad
    writes 5" bare, but in a class net list Freerouting 1.9 reads the number
    5 and then opens a string) or a line break (KiCad's reader and this one
    refuse it in a string). To keep a name KiCad already wrote, reuse its
    token text instead of re-quoting it."""
    _check_quote_char(quote_char)
    if quote_char in name:
        raise ValueError(f"{name!r} contains the quote character {quote_char!r}")
    if any(eol in name for eol in _LINE_BREAKS):
        raise ValueError(f"{name!r} contains a line break")
    if (not name or name[0] == "#" or "-" in name[1:]
            or any(c in _NEEDS_QUOTE for c in name)):
        return f"{quote_char}{name}{quote_char}"
    return name


def unquote(token: str, quote_char: str = DEFAULT_QUOTE) -> str:
    """The name a token spells: the quotes the tokenizer reads as quoting are
    dropped (DSN has no escapes) and every other character kept, so
    ``"Net-(D1-A)"`` -> Net-(D1-A), ``"R 1"-2`` -> R 1-2, ``5"`` -> 5".
    An unterminated quoted part raises DsnError."""
    pieces: list[str] = []
    i = 0
    while i < len(token):
        if _opens(token, i, 0, quote_char):
            close = _close(token, i, quote_char)
            pieces.append(token[i + 1:close])
            i = close + 1
        else:
            pieces.append(token[i])
            i += 1
    return "".join(pieces)


# ----------------------------------------------------------------------------
# List tree
# ----------------------------------------------------------------------------
def parse(text: str) -> list:
    """The one top-level list of ``text`` as nested lists of token text
    (atoms and quoted strings exactly as written). A fresh tree every call."""
    roots = parse_nodes(text)
    if len(roots) != 1:
        offset = roots[1].start if len(roots) > 1 else len(text)
        raise DsnError(f"expected one top-level list, found {len(roots)}", offset, text)
    return _as_list(roots[0])


def _as_list(node: Node) -> list:
    head = [node.name] if node.name else []
    return head + [_as_list(x) if isinstance(x, Node) else x.value for x in node.items]


def find(tree: Sequence, name: str) -> list | None:
    """The first direct child list of ``tree`` headed ``name``, or None."""
    return next((x for x in tree if isinstance(x, list) and x and x[0] == name), None)


def children(tree: Sequence, name: str) -> list[list]:
    """The direct child lists of ``tree`` headed ``name``, in order."""
    return [x for x in tree if isinstance(x, list) and x and x[0] == name]


def _atom_text(x: object) -> str:
    if not isinstance(x, str):
        raise TypeError(f"a tree holds str atoms and lists, got {type(x).__name__}: {x!r}")
    if not x:
        raise ValueError("an empty atom cannot be printed (write the empty name as quote(''))")
    return x


def _emit(tree: Tree, depth: int) -> str:
    if not isinstance(tree, (list, tuple)):
        return _atom_text(tree)
    parts = [_emit(x, depth + 1) for x in tree]
    first = next((k for k, x in enumerate(tree) if isinstance(x, (list, tuple))), len(tree))
    head = " ".join(parts[:first])
    pad = "\n" + "  " * (depth + 1)
    return "(" + head + "".join(pad + p for p in parts[first:]) + ")"


def emit(tree: Tree) -> str:
    """``tree`` (a parse result, or an atom) printed in the deterministic
    layout: the atoms before a list's first child list on its opening line,
    then every later element on its own line, indented 2 spaces per level;
    ')' closes on the last element's line. No trailing newline."""
    return _emit(tree, 0)
