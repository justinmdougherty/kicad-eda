#!/usr/bin/env python3
"""KiCad library tables and the project-library resolver (ROADMAP_DECISIONS D1).

Stdlib-only: the venv and KiCad's bundled Python (SWIG scripts) import it.

1. Tables: ``sym-lib-table`` / ``fp-lib-table`` in both row syntaxes KiCad
   ships, the quoted ``(lib (name "X") (type "KiCad") ...)`` and KiCad 9's old
   unquoted ``(lib (name X)(type Kicad)(uri ${KICAD9_...}/X.pretty)...)``. A
   rewrite keeps every row and item it does not own byte-for-byte, in place.
2. Resolution, D1 order: netlist ``libraries`` block > project table >
   stock (``<symbols>/<nick>.kicad_sym``, ``<footprints>/<nick>.pretty`` of
   the chosen install, which ``${KICAD<n>_*_DIR}`` for any n >= 6 aliases).
3. ``LibraryResolver.materialize`` merges the project libraries into the
   tables next to generated outputs (read by the KiCad 11 api-server and by
   ``kicad-cli sch erc`` beside a ``.kicad_pro``) as ``(descr "kicad-eda
   managed")`` rows, which a rerun replaces.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

SYMBOL, FOOTPRINT = "symbol", "footprint"
KINDS = (SYMBOL, FOOTPRINT)
NETLIST, PROJECT, STOCK = "netlist", "project", "stock"
SOURCES = (NETLIST, PROJECT, STOCK)

TABLE_FILES = {SYMBOL: "sym-lib-table", FOOTPRINT: "fp-lib-table"}
TABLE_TAGS = {SYMBOL: "sym_lib_table", FOOTPRINT: "fp_lib_table"}
MAP_KEYS = {SYMBOL: "symbols", FOOTPRINT: "footprints"}   # netlist block + library_map
LIB_SUFFIX = {SYMBOL: ".kicad_sym", FOOTPRINT: ".pretty"}
TABLE_VERSION = 7                     # what KiCad 7..11 write
MAP_VERSION = 1
MANAGED_DESCR = "kicad-eda managed"   # marks the rows materialize() owns
IMPORT_CACHE_OPTION = "kicad_import_cache"   # rows `kicad-cli ... import` writes
MIN_VERSIONED_VAR = 6                 # KICAD6_SYMBOL_DIR was the first numbered name
BLOCK_ORIGIN = "netlist libraries block"
# Libraries the builders use without a component naming them (design_nicknames):
POWER_LIBRARY = "power"               # power:PWR_FLAG (sch_build, sch_api), power:<net>
DEFAULT_MOUNTING_HOLE_FOOTPRINT = "MountingHole:MountingHole_3.2mm_M3"   # swig_build_board

_NICKNAME_RE = re.compile(r"^[^:\x00-\x1f]+$")   # same rule as netlist_schema
_TABLE_TAG_RE = re.compile(r"^[a-z_]+_lib_table$")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$\(([A-Za-z_][A-Za-z0-9_]*)\)")
_STOCK_VAR_RE = re.compile(r"KICAD(\d+)_(SYMBOL|FOOTPRINT|3DMODEL)_DIR")
_LEGACY_STOCK_VARS = {"KICAD_SYMBOL_DIR": "SYMBOL", "KISYSMOD": "FOOTPRINT",
                      "KISYS3DMOD": "3DMODEL"}
_ESCAPES = {'"': '"', "\\": "\\", "n": "\n", "t": "\t", "r": "\r",
            "a": "\a", "b": "\b", "f": "\f", "v": "\v"}
_ESCAPE_RE = re.compile(r"\\(.)", re.S)
# space | ( | ) | "quoted" (escapes allowed) | bare atom | a quote never closed
_TOKEN_RE = re.compile(r'(\s+)|(\()|(\))|("(?:[^"\\]|\\.)*")|([^\s()"]+)|(")', re.S)
_ROW_FIELDS = ("name", "type", "uri", "options", "descr")
_ROW_FLAGS = ("disabled", "hidden")
_PathArg = str | os.PathLike          # what the path parameters accept


class LibTableError(ValueError):
    """A library table, a libraries block or a materialize request is invalid."""


class LibraryNotFoundError(LibTableError, LookupError):
    """A nickname resolves nowhere (or only to a disabled row)."""


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


# --- tables ------------------------------------------------------------------
def _option_map(options: str) -> dict[str, str]:
    """KiCad row options ``key=value|flag`` -> {"key": "value", "flag": ""}."""
    out: dict[str, str] = {}
    for part in options.split("|"):
        if part.strip():
            key, _, value = part.partition("=")
            out[key.strip()] = value.strip()
    return out


def _is_import_cache(options: str) -> bool:
    value = _option_map(options).get(IMPORT_CACHE_OPTION)
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
        return _option_map(self.options)

    @property
    def import_cache(self) -> bool:
        return _is_import_cache(self.options)

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


def read_table(path: _PathArg) -> LibTable:
    """Parse a table file; errors name the file."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise LibTableError(f"cannot read library table {p}: {e}") from e
    return parse_table(text, source=str(p))


def _read_kind(path: Path, kind: str) -> LibTable:
    table = read_table(path)
    if table.tag != TABLE_TAGS[kind]:
        raise LibTableError(f"{path}: expected ({TABLE_TAGS[kind]} ...), found ({table.tag} ...)")
    return table


def format_table(table: LibTable) -> str:
    """Table text: KiCad 10/11 layout for new rows; parsed rows and unknown
    items verbatim, each item after as many rows as it followed."""
    n = len(table.rows)
    body = sorted([(min(at, n), 0, i, text) for i, (at, text) in enumerate(table.extra)]
                  + [(i, 1, i, row.text()) for i, row in enumerate(table.rows)])
    head = [f"({table.tag}"] + ([f"\t(version {table.version})"]
                                if table.version is not None else [])
    return "\n".join(head + [f"\t{item[3]}" for item in body] + [")"]) + "\n"


def write_table(path: _PathArg, table: LibTable) -> None:
    """Write atomically (temp file + replace), so KiCad never reads half a table."""
    p = Path(path)
    tmp = p.with_name(p.name + ".kicad-eda.tmp")
    tmp.write_text(format_table(table), encoding="utf-8", newline="\n")
    try:
        os.replace(tmp, p)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


# --- variable expansion --------------------------------------------------------
def expand_vars(text: str, *, kiprjmod: _PathArg | None = None,
                symbols_dir: _PathArg | None = None, footprints_dir: _PathArg | None = None,
                models_dir: _PathArg | None = None,
                env: Mapping[str, str] | None = None) -> tuple[str, tuple[str, ...]]:
    """Expand ``${VAR}`` / ``$(VAR)`` as a KiCad install would: ``KIPRJMOD``
    (when given), then ``KICAD<n>_SYMBOL_DIR`` / ``_FOOTPRINT_DIR`` /
    ``_3DMODEL_DIR`` for any n >= 6 and the KiCad 5 names ``KICAD_SYMBOL_DIR``
    / ``KISYSMOD`` / ``KISYS3DMOD`` (the chosen install beats a stale
    environment variable), then ``env`` (default ``os.environ``). Returns the
    text and the names left verbatim, in order of first appearance."""
    env = os.environ if env is None else env
    stock = {"SYMBOL": symbols_dir, "FOOTPRINT": footprints_dir, "3DMODEL": models_dir}
    missing: list[str] = []

    def lookup(name: str) -> str | None:
        if name == "KIPRJMOD" and kiprjmod is not None:
            return os.fspath(kiprjmod)
        m = _STOCK_VAR_RE.fullmatch(name)
        kind = (m.group(2) if m and int(m.group(1)) >= MIN_VERSIONED_VAR
                else _LEGACY_STOCK_VARS.get(name))
        if kind is not None and stock[kind] is not None:
            return os.fspath(stock[kind])
        return env.get(name)

    def substitute(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        value = lookup(name)
        if value is None:
            if name not in missing:
                missing.append(name)
            return m.group(0)
        return value

    return _VAR_RE.sub(substitute, text), tuple(missing)


# --- resolution ----------------------------------------------------------------
def _check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"library kind must be one of {KINDS}, got {kind!r}")
    return kind


def _abs(path: _PathArg) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _relative_intent(uri: str) -> bool:
    """A URI written relative to the project (``${KIPRJMOD}/...`` or a plain
    relative path) is rebased when copied elsewhere; anything else is absolute."""
    u = uri.strip()
    return u.startswith(("${KIPRJMOD}", "$(KIPRJMOD)")) or ("$" not in u and not os.path.isabs(u))


def _validate_block(block: object) -> dict[str, dict[str, str]]:
    """The netlist ``libraries`` block as {kind: {nick: path}}; fails fast."""
    out: dict[str, dict[str, str]] = {kind: {} for kind in KINDS}
    if block is None:
        return out
    if not isinstance(block, Mapping):
        raise LibTableError(f"the netlist 'libraries' block must be an object with 'symbols' / "
                            f"'footprints', got {type(block).__name__}")
    unknown = sorted(set(block) - set(MAP_KEYS.values()))
    if unknown:
        raise LibTableError(f"unknown key(s) {unknown} in the netlist 'libraries' block "
                            f"(allowed: 'symbols', 'footprints')")
    for kind, key in MAP_KEYS.items():
        entries = block.get(key)
        if entries is None:
            continue
        if not isinstance(entries, Mapping):
            raise LibTableError(f"libraries.{key} must be an object mapping nickname -> path, "
                                f"got {type(entries).__name__}")
        for nick, path in entries.items():
            if not isinstance(nick, str) or not _NICKNAME_RE.match(nick):
                raise LibTableError(f"invalid library nickname {nick!r} in libraries.{key}: a "
                                    f"nickname is non-empty, without ':' or control characters")
            if not isinstance(path, str) or not path.strip():
                raise LibTableError(f"libraries.{key} '{nick}': the path must be a non-empty "
                                    f"string, got {path!r}")
            out[kind][nick] = path
    return out


@dataclass(frozen=True)
class ResolvedLib:
    """A declared or resolved library: ``source`` netlist | project | stock,
    ``uri`` as written, ``origin`` the block or table file, ``managed`` a
    materialize() row, ``unresolved`` the variables the URI left verbatim."""
    kind: str
    nickname: str
    source: str
    path: Path
    uri: str
    origin: str = ""
    type: str = "KiCad"
    options: str = ""
    hidden: bool = False
    disabled: bool = False
    managed: bool = False
    unresolved: tuple[str, ...] = ()

    @property
    def exists(self) -> bool:
        return self.path.exists()

    @property
    def import_cache(self) -> bool:
        return _is_import_cache(self.options)


def write_minimal_kicad_pro(path: _PathArg) -> bool:
    """Create the smallest ``.kicad_pro`` that makes kicad-cli (9 and 11) read
    the project tables beside it. Never overwrites: returns False when the
    file already exists, True when it was written."""
    p = Path(path)
    if p.suffix != ".kicad_pro":
        raise ValueError(f"not a .kicad_pro path: {p}")
    try:
        with open(p, "x", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"meta": {"filename": p.name, "version": 1}}, indent=2) + "\n")
    except FileExistsError:
        return False
    return True


class LibraryResolver:
    """Nickname -> library path in the D1 order (netlist block > project table >
    stock). ``netlist_dir`` anchors the block (relative paths, ``${KIPRJMOD}``);
    ``project_dir`` (default ``netlist_dir``) holds the project tables; stock
    dirs may be None; ``env`` (default ``os.environ``) supplies other vars.

    ``design_libraries`` is the netlist's ``libraries`` block, ``{}`` for a
    netlist without one (for_netlist / for_design pass that): project rows
    tagged ``kicad-eda managed`` are then a block's derived copies and are
    ignored (the next materialize replaces or removes them). ``None`` means
    no netlist (``dir_only``, e.g. a schematic directory): every row counts,
    as KiCad reads the tables. A disabled project row hides the nickname, as
    in KiCad. A malformed block or project table raises LibTableError here."""

    write_minimal_kicad_pro = staticmethod(write_minimal_kicad_pro)

    def __init__(self, netlist_dir: _PathArg, design_libraries: Mapping | None,
                 stock_symbols_dir: _PathArg | None, stock_footprints_dir: _PathArg | None,
                 project_dir: _PathArg | None = None, *,
                 env: Mapping[str, str] | None = None) -> None:
        self.netlist_dir = _abs(netlist_dir)
        self.project_dir = _abs(project_dir) if project_dir is not None else self.netlist_dir
        self.stock_dirs: dict[str, Path | None] = {
            SYMBOL: _abs(stock_symbols_dir) if stock_symbols_dir is not None else None,
            FOOTPRINT: _abs(stock_footprints_dir) if stock_footprints_dir is not None else None}
        self.dir_only = design_libraries is None
        self._env = dict(os.environ if env is None else env)
        block = _validate_block(design_libraries)
        self._load_errors: list[str] = []
        self._derived: dict[str, set[str]] = {kind: set() for kind in KINDS}
        self._block = {kind: self._block_decls(kind, block[kind]) for kind in KINDS}
        self._project = {kind: self._project_decls(kind) for kind in KINDS}
        self._conflicts = [msg for kind in KINDS for msg in self._find_conflicts(kind)]

    @classmethod
    def for_design(cls, netlist_dir: _PathArg, design: Mapping,
                   stock_symbols_dir: _PathArg | None, stock_footprints_dir: _PathArg | None,
                   project_dir: _PathArg | None = None, *,
                   env: Mapping[str, str] | None = None) -> "LibraryResolver":
        """A resolver for a loaded netlist: its ``libraries`` block, or ``{}``
        when it has none (earlier managed rows are then stale, not libraries)."""
        if not isinstance(design, Mapping):
            raise LibTableError(f"the netlist must be a JSON object, got {type(design).__name__}")
        block = design.get("libraries")
        return cls(netlist_dir, {} if block is None else block, stock_symbols_dir,
                   stock_footprints_dir, project_dir, env=env)

    @classmethod
    def for_netlist(cls, netlist_path: _PathArg,
                    stock_symbols_dir: _PathArg | None, stock_footprints_dir: _PathArg | None,
                    project_dir: _PathArg | None = None, *,
                    env: Mapping[str, str] | None = None) -> "LibraryResolver":
        """A resolver for a netlist.json file (its directory and its block)."""
        path = Path(netlist_path)
        try:
            design = json.loads(path.read_text(encoding="utf-8-sig"))   # Notepad's BOM too
        except (OSError, ValueError) as e:
            raise LibTableError(f"cannot read netlist {path}: {e}") from e
        if not isinstance(design, dict):
            raise LibTableError(f"netlist {path} is not a JSON object")
        return cls.for_design(path.parent, design, stock_symbols_dir, stock_footprints_dir,
                              project_dir, env=env)

    # --- loading ---------------------------------------------------------------
    def _locate(self, uri: str, base: Path) -> tuple[Path, tuple[str, ...]]:
        """Expand a URI with ``${KIPRJMOD}`` = base; a relative result is taken
        relative to base."""
        text, missing = expand_vars(uri, kiprjmod=base, symbols_dir=self.stock_dirs[SYMBOL],
                                    footprints_dir=self.stock_dirs[FOOTPRINT], env=self._env)
        path = Path(text)
        if not path.is_absolute():
            path = base / path
        return Path(os.path.normpath(path)), missing

    def _block_decls(self, kind: str, entries: dict[str, str]) -> dict[str, ResolvedLib]:
        decls = {}
        for nick, uri in entries.items():
            path, missing = self._locate(uri, self.netlist_dir)
            decls[nick] = ResolvedLib(kind, nick, NETLIST, path, uri, BLOCK_ORIGIN,
                                      unresolved=missing)
        return decls

    def _project_decls(self, kind: str) -> dict[str, ResolvedLib]:
        table_path = self.project_dir / TABLE_FILES[kind]
        if not table_path.is_file():
            return {}
        decls: dict[str, ResolvedLib] = {}
        for row in _read_kind(table_path, kind).rows:
            if row.managed and not self.dir_only:     # a copy of a block: the block decides
                self._derived[kind].add(row.name)
            elif row.name in decls:
                self._load_errors.append(
                    f"{kind} library '{row.name}' is defined twice in {table_path}: duplicate "
                    f"nickname (KiCad refuses to load such a table)")
            else:
                path, missing = self._locate(row.uri, self.project_dir)
                decls[row.name] = ResolvedLib(kind, row.name, PROJECT, path, row.uri,
                                              str(table_path), row.type, row.options, row.hidden,
                                              row.disabled, row.managed, missing)
        return decls

    def _find_conflicts(self, kind: str) -> Iterator[str]:
        for nick, ours in self._block[kind].items():
            theirs = self._project[kind].get(nick)
            if theirs is None:
                continue
            if not _same_path(ours.path, theirs.path):
                yield (f"{kind} library '{nick}': the netlist libraries block points to "
                       f"{ours.path} but {theirs.origin} points to {theirs.path} (same "
                       f"nickname, different URI)")
            elif theirs.disabled:
                yield (f"{kind} library '{nick}': the netlist libraries block declares "
                       f"{ours.path} but {theirs.origin} has that nickname disabled, so KiCad "
                       f"would not load it (enable or remove the row)")

    def _decls(self, kind: str) -> list[ResolvedLib]:
        """Block declarations, then project rows the block does not shadow."""
        block = self._block[kind]
        return list(block.values()) + [d for n, d in self._project[kind].items() if n not in block]

    def _decl(self, kind: str, nick: str) -> ResolvedLib | None:
        return self._block[kind].get(nick) or self._project[kind].get(nick)

    def _stock_path(self, kind: str, nick: str) -> Path | None:
        root = self.stock_dirs[kind]
        if root is None:
            return None
        path = root / f"{nick}{LIB_SUFFIX[kind]}"
        return path if (path.is_file() if kind == SYMBOL else path.is_dir()) else None

    # --- lookups -----------------------------------------------------------------
    def resolve(self, kind: str, nick: str) -> ResolvedLib | None:
        """Where ``nick`` resolves, or None. A declared library resolves even if
        its file is missing (errors() says so); it never falls through to stock."""
        decl = self._decl(_check_kind(kind), nick)
        if decl is not None:
            return None if decl.disabled else decl
        path = self._stock_path(kind, nick)
        if path is None:
            return None
        return ResolvedLib(kind, nick, STOCK, path, path.as_posix(),
                           f"stock {kind} libraries ({self.stock_dirs[kind]})")

    def _explain(self, kind: str, nick: str) -> str:
        decl = self._decl(kind, nick)
        if decl is not None and decl.disabled:
            return f"{kind} library '{nick}' is disabled in {decl.origin}"
        root, table = self.stock_dirs[kind], self.project_dir / TABLE_FILES[kind]
        stock = (f"{root / (nick + LIB_SUFFIX[kind])} does not exist" if root is not None
                 else f"no stock {kind} directory is configured")
        project = (f"{table} has it only as a '{MANAGED_DESCR}' row, ignored because the block "
                   f"no longer declares it (the next materialize removes it)"
                   if nick in self._derived[kind] else f"not in {table}")
        return (f"{kind} library '{nick}' not found: it is not in the netlist libraries block, "
                f"{project}, and {stock}")

    def _require(self, kind: str, nick: str) -> ResolvedLib:
        hit = self.resolve(kind, nick)
        if hit is None:
            raise LibraryNotFoundError(self._explain(kind, nick))
        return hit

    def symbol_lib_path(self, nick: str) -> Path:
        """The .kicad_sym for ``nick``; raises LibraryNotFoundError."""
        return self._require(SYMBOL, nick).path

    def footprint_lib_path(self, nick: str) -> Path:
        """The .pretty directory for ``nick``; raises LibraryNotFoundError."""
        return self._require(FOOTPRINT, nick).path

    def source(self, nick: str, kind: str | None = None) -> str:
        """'netlist' | 'project' | 'stock'. Without ``kind`` the symbol library
        answers first, then the footprint library. Raises LibraryNotFoundError."""
        kinds = KINDS if kind is None else (_check_kind(kind),)
        for k in kinds:
            hit = self.resolve(k, nick)
            if hit is not None:
                return hit.source
        raise LibraryNotFoundError("; ".join(self._explain(k, nick) for k in kinds))

    # --- diagnostics ------------------------------------------------------------
    def _problem(self, decl: ResolvedLib) -> str | None:
        if decl.unresolved:
            names = ", ".join("${" + v + "}" for v in decl.unresolved)
            return f"undefined variable(s) {names} in {decl.uri!r}"
        suffix = LIB_SUFFIX[decl.kind]
        if decl.source == NETLIST and decl.path.suffix.lower() != suffix:
            return f"{decl.path} is not a {suffix} {decl.kind} library"
        if not decl.path.exists():
            return f"{decl.path} does not exist"
        if decl.type.lower() == "kicad":
            if decl.kind == SYMBOL and not decl.path.is_file():
                return f"{decl.path} is not a file (a KiCad symbol library is one .kicad_sym file)"
            if decl.kind == FOOTPRINT and not decl.path.is_dir():
                return (f"{decl.path} is not a directory (a KiCad footprint library is a "
                        f".pretty folder)")
        return None

    def errors(self, symbols: Iterable[str] = (), footprints: Iterable[str] = ()) -> list[str]:
        """Every library problem as a message: duplicate project nicknames,
        block-vs-project conflicts (another URI or a disabled row: D1 lint
        error), undefined variables, missing or wrong-kind files (not for
        disabled rows), and each ``symbols`` / ``footprints`` nickname unresolved."""
        out = list(self._load_errors) + list(self._conflicts)
        for kind in KINDS:
            for decl in self._decls(kind):
                problem = None if decl.disabled else self._problem(decl)
                if problem:
                    out.append(f"{kind} library '{decl.nickname}' ({decl.origin}): {problem}")
        for kind, nicks in ((SYMBOL, symbols), (FOOTPRINT, footprints)):
            for nick in dict.fromkeys(nicks):
                if self.resolve(kind, nick) is not None:
                    continue
                known = self._decl(kind, nick) is not None or nick in self._derived[kind]
                out.append(self._explain(kind, nick) if known else
                           f"{kind} library '{nick}' is not in the netlist libraries block, the "
                           f"project {TABLE_FILES[kind]} or the stock {kind} libraries")
        return out

    def library_map(self) -> dict:
        """JSON-serialisable map for consumers without this class (the SWIG
        builder reads it back with lookup()): every block and project library,
        plus the stock dirs that resolve the rest by naming convention."""
        out: dict = {"version": MAP_VERSION, "netlist_dir": str(self.netlist_dir),
                     "project_dir": str(self.project_dir),
                     "stock": {MAP_KEYS[k]: (str(d) if d is not None else None)
                               for k, d in self.stock_dirs.items()}}
        for kind in KINDS:
            out[MAP_KEYS[kind]] = {
                d.nickname: {"path": str(d.path), "source": d.source, "type": d.type,
                             "uri": d.uri, "options": d.options, "import_cache": d.import_cache,
                             "exists": d.exists, "disabled": d.disabled, "managed": d.managed,
                             "origin": d.origin}
                for d in self._decls(kind)}
        out["errors"] = self.errors()
        return out

    # --- materialize --------------------------------------------------------------
    def _managed_uri(self, decl: ResolvedLib, out_dir: Path) -> str:
        if decl.unresolved:
            return decl.uri            # KiCad may know the variable; keep it as written
        if _relative_intent(decl.uri):
            try:
                rel = os.path.relpath(decl.path, out_dir)
            except ValueError:         # another drive (Windows): only absolute works
                return decl.path.as_posix()
            return "${KIPRJMOD}/" + Path(rel).as_posix()
        return decl.path.as_posix()

    def _check_clash(self, path: Path, decl: ResolvedLib, clash: LibRow, out_dir: Path) -> None:
        """``clash``, an unmanaged row of the output table, holds ``decl``'s
        nickname: fine only for the same library, enabled or disabled alike."""
        clash_path, _ = self._locate(clash.uri, out_dir)
        if not (_same_path(clash_path, decl.path) or clash.uri == decl.uri):
            problem = f"pointing to {clash_path}; rename one of them"
        elif clash.disabled and not decl.disabled:
            problem = ("for that library, but disabled there, so KiCad would not load it; "
                       "enable or remove that row")
        elif decl.disabled and not clash.disabled:
            problem = (f"for that library, but enabled there while {decl.origin} disables "
                       f"it; disable or remove that row")
        else:
            return
        raise LibTableError(f"{path}: cannot add {decl.kind} library '{decl.nickname}' "
                            f"({decl.path}, from {decl.origin}): the table already has an "
                            f"unmanaged row '{decl.nickname}' {problem}")

    def _plan(self, kind: str, out_dir: Path,
              wanted: Iterable[str] | None) -> tuple[Path, LibTable | None, dict]:
        """The table to write (None = leave the file alone) and its report."""
        path = out_dir / TABLE_FILES[kind]
        existing = _read_kind(path, kind) if path.exists() else LibTable(TABLE_TAGS[kind])
        keep = [r for r in existing.rows if not r.managed]
        old = [r for r in existing.rows if r.managed]
        names = None if wanted is None else set(wanted)
        new: list[LibRow] = []
        skipped: dict[str, str] = {}
        for decl in sorted(self._decls(kind), key=lambda d: (d.nickname.lower(), d.nickname)):
            if names is not None and decl.nickname not in names:
                continue
            clash = next((r for r in keep if r.name == decl.nickname), None)
            if clash is not None:
                self._check_clash(path, decl, clash, out_dir)
                skipped[decl.nickname] = f"already in {path} as an unmanaged row"
                continue
            new.append(LibRow(decl.nickname, self._managed_uri(decl, out_dir), type=decl.type,
                              options=decl.options, descr=MANAGED_DESCR,
                              disabled=decl.disabled, hidden=decl.hidden))
        report = {"path": str(path), "written": new != old, "managed": [r.name for r in new],
                  "removed": sorted({r.name for r in old} - {r.name for r in new}),
                  "skipped": skipped}
        if new == old:
            return path, None, report
        extra = tuple((sum(not r.managed for r in existing.rows[:at]), text)   # same place
                      for at, text in existing.extra)                       # among kept rows
        return path, LibTable(existing.tag, tuple(keep + new), existing.version, extra), report

    def materialize(self, out_dir: _PathArg, symbols: Iterable[str] | None = None,
                    footprints: Iterable[str] | None = None) -> dict:
        """Merge the declared libraries (block entries, project rows; narrowed
        by the ``symbols`` / ``footprints`` nicknames; never stock) into
        ``out_dir``'s tables as managed rows keeping their disabled/hidden
        state, URIs rebased onto ``${KIPRJMOD}`` = ``out_dir``. Unmanaged rows
        and items stay byte-for-byte in place; one with the same nickname is
        reused if it names the same library in the same state, else
        LibTableError (as for a block-vs-project conflict). Old managed rows
        are replaced, new ones go last: a rerun writes nothing, and without
        project libraries no file is created (stale managed rows are cleared).
        Both tables are checked before either is written. Returns a report."""
        out = _abs(out_dir)
        if not out.is_dir():
            raise LibTableError(f"cannot materialize library tables: output directory {out} "
                                f"does not exist")
        if self._conflicts:
            raise LibTableError("cannot materialize library tables: " + "; ".join(self._conflicts))
        wanted = {SYMBOL: symbols, FOOTPRINT: footprints}
        plans = [self._plan(kind, out, wanted[kind]) for kind in KINDS]
        report: dict = {"out_dir": str(out)}
        for path, table, table_report in plans:
            if table is not None:
                write_table(path, table)
            report[path.name] = table_report
        return report


# --- helpers for consumers -----------------------------------------------------
def lookup(library_map: Mapping, kind: str, nick: str) -> str | None:
    """Resolve ``nick`` from a library_map() dict (e.g. read back from JSON in
    a SWIG script): its entry, else the stock directory by naming convention;
    None when neither has it or the entry is disabled."""
    key = MAP_KEYS[_check_kind(kind)]
    entry = (library_map.get(key) or {}).get(nick)
    if entry is not None:
        return None if entry.get("disabled") else entry["path"]
    root = (library_map.get("stock") or {}).get(key)
    if not root:
        return None
    path = os.path.join(root, nick + LIB_SUFFIX[kind])
    return path if (os.path.isfile(path) if kind == SYMBOL else os.path.isdir(path)) else None


def design_nicknames(design: Mapping) -> dict[str, list[str]]:
    """Library nicknames a netlist build uses, ``{"symbols": [...], "footprints":
    [...]}`` sorted: component ``lib_id`` / ``footprint``, POWER_LIBRARY (every
    schematic gets power:PWR_FLAG) and the mounting-hole footprint when there
    are holes (``mounting_holes.footprint``, else DEFAULT_MOUNTING_HOLE_FOOTPRINT
    as swig_build_board). Feed it to ``errors(**...)`` / ``materialize(out, **...)``."""
    found: dict[str, set[str]] = {SYMBOL: {POWER_LIBRARY}, FOOTPRINT: set()}

    def add(kind: str, lib_ref: object) -> None:
        if isinstance(lib_ref, str) and ":" in lib_ref:
            found[kind].add(lib_ref.split(":", 1)[0])

    for comp in design.get("components") or []:
        if isinstance(comp, Mapping):
            add(SYMBOL, comp.get("lib_id"))
            add(FOOTPRINT, comp.get("footprint"))
    holes = design.get("mounting_holes")
    if holes:
        spec = holes if isinstance(holes, Mapping) else {}
        add(FOOTPRINT, spec.get("footprint", DEFAULT_MOUNTING_HOLE_FOOTPRINT))
    return {MAP_KEYS[kind]: sorted(names) for kind, names in found.items()}
