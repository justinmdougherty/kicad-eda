#!/usr/bin/env python3
"""The S-expression layer of KiCad library tables (split out of libtable).

Stdlib-only and Python 3.11-safe: the venv and KiCad's bundled Python (SWIG
scripts) import it, through libtable, which re-exports everything here.

- ``sym-lib-table`` / ``fp-lib-table`` in both row syntaxes KiCad ships, the
  quoted ``(lib (name "X") (type "KiCad") ...)`` and KiCad 9's old unquoted
  ``(lib (name X)(type Kicad)(uri ${KICAD9_...}/X.pretty)...)``. A rewrite
  keeps every row and item it does not own byte-for-byte, in place.
- ``header_value``: one ``(tag value)`` item of a KiCad file's header, e.g.
  the ``(generator "kicad-eda")`` of a .kicad_sym / .kicad_mod.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

TABLE_VERSION = 7                     # what KiCad 7..11 write
MANAGED_DESCR = "kicad-eda managed"   # marks the rows libtable's materialize() owns
IMPORT_CACHE_OPTION = "kicad_import_cache"   # rows `kicad-cli ... import` writes

_TABLE_TAG_RE = re.compile(r"^[a-z_]+_lib_table$")
_ESCAPES = {'"': '"', "\\": "\\", "n": "\n", "t": "\t", "r": "\r",
            "a": "\a", "b": "\b", "f": "\f", "v": "\v"}
_ESCAPE_RE = re.compile(r"\\(.)", re.S)
# space | ( | ) | "quoted" (escapes allowed) | bare atom | a quote never closed
_TOKEN_RE = re.compile(r'(\s+)|(\()|(\))|("(?:[^"\\]|\\.)*")|([^\s()"]+)|(")', re.S)
_ROW_FIELDS = ("name", "type", "uri", "options", "descr")
_ROW_FLAGS = ("disabled", "hidden")
PathArg = str | os.PathLike           # what the path parameters accept


class LibTableError(ValueError):
    """A library table, a libraries block or a materialize request is invalid."""


# --- S-expression reader (just enough for library tables, with positions) ---
@dataclass
class _Atom:
    value: str
    pos: int


@dataclass
class _List:
    items: list
    start: int
    end: int = -1


def _syntax(source: str, text: str, pos: int, problem: str) -> LibTableError:
    line, col = text.count("\n", 0, pos) + 1, pos - text.rfind("\n", 0, pos)
    return LibTableError(f"{source}: line {line}, col {col}: {problem}")


def _unquote(token: str) -> str:
    """A quoted token's value. Unknown backslash pairs stay as written, so an
    unescaped Windows path survives."""
    return _ESCAPE_RE.sub(lambda m: _ESCAPES.get(m.group(1), m.group(0)), token[1:-1])


def _read_sexp(text: str, source: str) -> _List:
    """Parse exactly one top-level list."""
    stack: list[_List] = []
    root: _List | None = None
    for m in _TOKEN_RE.finditer(text):
        space, opening, closing, quoted, bare, _lone_quote = m.groups()
        pos = m.start()
        if space:
            continue
        if closing:
            if not stack:
                raise _syntax(source, text, pos, "unbalanced ')'")
            stack.pop().end = m.end()
        elif root is not None and not stack:
            raise _syntax(source, text, pos, "unexpected text after the table")
        elif opening:
            node = _List([], pos)
            if stack:
                stack[-1].items.append(node)
            else:
                root = node
            stack.append(node)
        elif not stack:
            raise _syntax(source, text, pos, "expected '(' to start the table")
        elif quoted is None and bare is None:
            raise _syntax(source, text, pos, "unterminated string")
        else:
            stack[-1].items.append(_Atom(bare if quoted is None else _unquote(quoted), pos))
    if stack:
        raise _syntax(source, text, stack[-1].start, "unclosed '(' (missing ')')")
    if root is None:
        raise LibTableError(f"{source}: empty file (expected a library table)")
    return root


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def header_value(text: str, tag: str) -> str | None:
    """The value of the first ``(tag value)`` item directly inside the
    top-level list of ``text`` (quoted or bare), e.g. ``(generator
    "kicad-eda")`` in a .kicad_sym / .kicad_mod header; None when there is
    none. Deeper lists never count. ``text`` may be just the head of a file:
    an unfinished list or string ends the scan; so does a leading atom."""
    depth = 0
    item: list[str] | None = None        # the atoms of the depth-2 list being read
    for m in _TOKEN_RE.finditer(text.lstrip("\ufeff")):
        space, opening, closing, quoted, bare, lone_quote = m.groups()
        if space:
            continue
        if lone_quote is not None or (depth == 0 and not opening):
            return None                  # an unterminated string / not a list
        if opening:
            depth += 1
            item = [] if depth == 2 else None if depth > 2 else item
        elif closing:
            if depth == 2 and item is not None and len(item) == 2 and item[0] == tag:
                return item[1]
            depth -= 1
            if depth == 0:
                return None              # the root list ended
        elif depth == 2 and item is not None:
            item.append(bare if quoted is None else _unquote(quoted))
    return None


# --- tables ------------------------------------------------------------------
def parse_options(options: str) -> dict[str, str]:
    """KiCad row options ``key=value|flag`` -> {"key": "value", "flag": ""}."""
    out: dict[str, str] = {}
    for part in options.split("|"):
        if part.strip():
            key, _, value = part.partition("=")
            out[key.strip()] = value.strip()
    return out


def is_import_cache(options: str) -> bool:
    """A row ``kicad-cli ... import`` wrote (``kicad_import_cache=1``)."""
    value = parse_options(options).get(IMPORT_CACHE_OPTION)
    return value is not None and value.lower() not in ("0", "false", "no")


@dataclass(frozen=True)
class LibRow:
    """One ``(lib ...)`` row. ``raw`` is the exact source text of a parsed row
    (unknown tokens included); it is not part of equality."""
    name: str
    uri: str
    type: str = "KiCad"
    options: str = ""
    descr: str = ""
    disabled: bool = False
    hidden: bool = False
    raw: str | None = field(default=None, compare=False, repr=False)

    @property
    def is_kicad(self) -> bool:
        """KiCad-format library (KiCad 9 tables still spell one row "Kicad")."""
        return self.type.lower() == "kicad"

    @property
    def managed(self) -> bool:
        return self.descr == MANAGED_DESCR

    @property
    def option_map(self) -> dict[str, str]:
        return parse_options(self.options)

    @property
    def import_cache(self) -> bool:
        return is_import_cache(self.options)

    def to_sexp(self) -> str:
        """The row in the KiCad 10/11 layout (``raw`` is ignored)."""
        parts = [f"({tag} {_quote(value)})" for tag, value in (
            ("name", self.name), ("type", self.type), ("uri", self.uri),
            ("options", self.options), ("descr", self.descr))]
        parts += [f"({flag})" for flag in _ROW_FLAGS if getattr(self, flag)]
        return "(lib " + " ".join(parts) + ")"

    def text(self) -> str:
        """What a written table carries for this row: the source text if parsed."""
        return self.raw if self.raw is not None else self.to_sexp()


@dataclass(frozen=True)
class LibTable:
    """A parsed table. ``version`` is None for tables written without one
    (KiCad 9's global tables). ``extra`` keeps unknown top-level items
    verbatim as ``(rows_before, text)``, so a rewrite puts each one back."""
    tag: str
    rows: tuple[LibRow, ...] = ()
    version: int | None = TABLE_VERSION
    extra: tuple[tuple[int, str], ...] = ()


def _parse_row(node: _List, text: str, source: str) -> LibRow:
    fields: dict[str, str] = {}
    flags: dict[str, bool] = {}
    for item in node.items[1:]:
        pos = item.pos if isinstance(item, _Atom) else item.start
        if not (isinstance(item, _List) and item.items and isinstance(item.items[0], _Atom)):
            raise _syntax(source, text, pos, "malformed lib row: every entry is (token value)")
        tag, args = item.items[0].value, item.items[1:]
        if tag in _ROW_FIELDS:
            if tag in fields:
                raise _syntax(source, text, pos, f"duplicate ({tag} ...) in a lib row")
            if len(args) != 1 or not isinstance(args[0], _Atom):
                raise _syntax(source, text, pos, f"malformed ({tag} ...): expected one value")
            fields[tag] = args[0].value
        elif tag in _ROW_FLAGS:    # KiCad writes bare (disabled) / (hidden)
            if args:
                raise _syntax(source, text, pos, f"malformed ({tag}): takes no value")
            flags[tag] = True
        # any other token survives through LibRow.raw
    name = fields.get("name")
    if name is None:
        raise _syntax(source, text, node.start, "malformed lib row: no (name ...)")
    for required in ("type", "uri"):
        if required not in fields:
            raise _syntax(source, text, node.start, f"lib row {name!r} has no ({required} ...)")
    return LibRow(name=name, uri=fields["uri"], type=fields["type"],
                  options=fields.get("options", ""), descr=fields.get("descr", ""),
                  disabled=flags.get("disabled", False), hidden=flags.get("hidden", False),
                  raw=text[node.start:node.end])


def parse_table(text: str, source: str = "<library table>") -> LibTable:
    """Parse a sym-lib-table / fp-lib-table (either row syntax). Raises
    LibTableError naming ``source`` and the line/column of the problem."""
    root = _read_sexp(text, source)
    head = root.items[0] if root.items else None
    if not (isinstance(head, _Atom) and _TABLE_TAG_RE.match(head.value)):
        found = head.value if isinstance(head, _Atom) else "?"
        raise _syntax(source, text, root.start,
                      f"not a library table: starts with ({found} ...), expected "
                      f"(sym_lib_table ...) or (fp_lib_table ...)")
    rows: list[LibRow] = []
    extra: list[tuple[int, str]] = []
    version: int | None = None
    for item in root.items[1:]:
        if isinstance(item, _Atom):
            raise _syntax(source, text, item.pos, f"malformed table: unexpected {item.value!r}")
        tag = item.items[0].value if item.items and isinstance(item.items[0], _Atom) else None
        if tag == "lib":
            rows.append(_parse_row(item, text, source))
        elif tag == "version":
            value = item.items[1].value if len(item.items) == 2 and isinstance(
                item.items[1], _Atom) else ""
            if not value.isdigit():
                raise _syntax(source, text, item.start, f"bad (version ...) value {value!r}")
            version = int(value)
        else:
            extra.append((len(rows), text[item.start:item.end]))
    return LibTable(head.value, tuple(rows), version, tuple(extra))


def read_table(path: PathArg) -> LibTable:
    """Parse a table file; errors name the file."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise LibTableError(f"cannot read library table {p}: {e}") from e
    return parse_table(text, source=str(p))


def format_table(table: LibTable) -> str:
    """Table text: KiCad 10/11 layout for new rows; parsed rows and unknown
    items verbatim, each item after as many rows as it followed."""
    n = len(table.rows)
    body = sorted([(min(at, n), 0, i, text) for i, (at, text) in enumerate(table.extra)]
                  + [(i, 1, i, row.text()) for i, row in enumerate(table.rows)])
    head = [f"({table.tag}"] + ([f"\t(version {table.version})"]
                                if table.version is not None else [])
    return "\n".join(head + [f"\t{item[3]}" for item in body] + [")"]) + "\n"


def write_table(path: PathArg, table: LibTable) -> None:
    """Write atomically (temp file + replace), so KiCad never reads half a table."""
    p = Path(path)
    tmp = p.with_name(p.name + ".kicad-eda.tmp")
    tmp.write_text(format_table(table), encoding="utf-8", newline="\n")
    try:
        os.replace(tmp, p)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
