#!/usr/bin/env python3
"""KiCad library tables and the project-library resolver (ROADMAP_DECISIONS D1).

Stdlib-only and Python 3.11-safe: the venv and KiCad's bundled Python (SWIG
scripts) import it.

1. Tables: the S-expression layer (both row syntaxes KiCad ships; a rewrite
   keeps every row and item it does not own byte-for-byte, in place) lives in
   libtable_sexp and is re-exported here: ``libtable.read_table``, ``LibRow``,
   ``LibTable``, ``LibTableError``... keep working.
2. Resolution, D1 order: netlist ``libraries`` block > project table >
   stock (``<symbols>/<nick>.kicad_sym``, ``<footprints>/<nick>.pretty`` of
   the chosen install, which ``${KICAD<n>_*_DIR}`` for any n >= 6 aliases).
   ``LibraryResolver.for_directory`` reads a directory that has no netlist
   (a schematic's, an import's) the way KiCad does.
3. ``LibraryResolver.materialize`` merges the project libraries into the
   tables next to generated outputs (read by the KiCad 11 api-server and by
   ``kicad-cli sch erc`` beside a ``.kicad_pro``) as ``(descr "kicad-eda
   managed")`` rows, which a rerun replaces.
4. ``library_generator`` tells the libraries this plugin authored
   (``(generator "kicad-eda")``) from stock and hand-made ones.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from libtable_sexp import (  # noqa: F401  (the table layer, re-exported for consumers)
    IMPORT_CACHE_OPTION, MANAGED_DESCR, TABLE_VERSION, LibRow, LibTable, LibTableError,
    format_table, header_value, is_import_cache, parse_options, parse_table, read_table,
    write_table)
from libtable_sexp import PathArg as _PathArg

SYMBOL, FOOTPRINT = "symbol", "footprint"
KINDS = (SYMBOL, FOOTPRINT)
NETLIST, PROJECT, STOCK = "netlist", "project", "stock"
SOURCES = (NETLIST, PROJECT, STOCK)

TABLE_FILES = {SYMBOL: "sym-lib-table", FOOTPRINT: "fp-lib-table"}
TABLE_TAGS = {SYMBOL: "sym_lib_table", FOOTPRINT: "fp_lib_table"}
MAP_KEYS = {SYMBOL: "symbols", FOOTPRINT: "footprints"}   # netlist block + library_map
LIB_SUFFIX = {SYMBOL: ".kicad_sym", FOOTPRINT: ".pretty"}
MAP_VERSION = 1
MIN_VERSIONED_VAR = 6                 # KICAD6_SYMBOL_DIR was the first numbered name
BLOCK_ORIGIN = "netlist libraries block"
# Libraries the builders use without a component naming them (design_nicknames):
POWER_LIBRARY = "power"               # power:PWR_FLAG (sch_build, sch_api), power:<net>
DEFAULT_MOUNTING_HOLE_FOOTPRINT = "MountingHole:MountingHole_3.2mm_M3"   # swig_build_board
KICAD_EDA_GENERATOR = "kicad-eda"     # the (generator ...) fp_author / sym_author write
HEADER_BYTES = 8192                   # a library file's header fits; whole libs are not read

_NICKNAME_RE = re.compile(r"^[^:\x00-\x1f]+$")   # same rule as netlist_schema
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$\(([A-Za-z_][A-Za-z0-9_]*)\)")
_STOCK_VAR_RE = re.compile(r"KICAD(\d+)_(SYMBOL|FOOTPRINT|3DMODEL)_DIR")
_LEGACY_STOCK_VARS = {"KICAD_SYMBOL_DIR": "SYMBOL", "KISYSMOD": "FOOTPRINT",
                      "KISYS3DMOD": "3DMODEL"}


class LibraryNotFoundError(LibTableError, LookupError):
    """A nickname resolves nowhere (or only to a disabled row)."""


def _read_kind(path: Path, kind: str) -> LibTable:
    table = read_table(path)
    if table.tag != TABLE_TAGS[kind]:
        raise LibTableError(f"{path}: expected ({TABLE_TAGS[kind]} ...), found ({table.tag} ...)")
    return table


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


def _location_independent(uri: str) -> bool:
    """A URI naming the same file whichever table holds it: an absolute path
    or an install / user variable; never relative or ``${KIPRJMOD}``-based."""
    return not _relative_intent(uri) and "KIPRJMOD" not in uri


def _same_file(a: Path, b: Path) -> bool:
    """The same library location: equal normalised paths, or equal real paths
    (symlinks and junctions resolved)."""
    if _same_path(a, b):
        return True
    try:
        return _same_path(Path(os.path.realpath(a)), Path(os.path.realpath(b)))
    except (OSError, ValueError):      # unresolvable: not provably the same file
        return False


def _check_nicknames(arg: str, value: object) -> None:
    """Refuse a bare str / bytes where an iterable of nicknames is expected:
    iterating 'Device' would ask for the libraries 'D', 'e', 'v', ..."""
    if isinstance(value, (str, bytes)):
        raise TypeError(f"{arg}= takes an iterable of library nicknames, not a bare "
                        f"{type(value).__name__}: {value!r} would be read one character at "
                        f"a time; pass [{value!r}]")


def _validate_block(block: object) -> dict[str, dict[str, str]]:
    """The netlist ``libraries`` block as {kind: {nick: path}}; fails fast."""
    out: dict[str, dict[str, str]] = {kind: {} for kind in KINDS}
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
        return is_import_cache(self.options)


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


_DIR_ONLY: Mapping = MappingProxyType({})   # for_directory's marker: no netlist, no block


class LibraryResolver:
    """Nickname -> library path in the D1 order (netlist block > project table >
    stock). ``netlist_dir`` anchors the block (relative paths, ``${KIPRJMOD}``);
    ``project_dir`` (default ``netlist_dir``) holds the project tables; stock
    dirs may be None; ``env`` (default ``os.environ``) supplies other vars.

    ``design_libraries`` is the netlist's ``libraries`` block, ``{}`` for a
    netlist without one (for_netlist / for_design pass that): project rows
    tagged ``kicad-eda managed`` are then a block's derived copies and are
    ignored (the next materialize replaces or removes them). ``None`` is a
    TypeError: a directory without a netlist (a schematic's, an import's) is
    read by ``for_directory`` (``dir_only``), where every row counts, as KiCad
    reads the tables. A disabled project row hides the nickname, as in
    KiCad. A malformed block or project table raises LibTableError here."""

    write_minimal_kicad_pro = staticmethod(write_minimal_kicad_pro)

    def __init__(self, netlist_dir: _PathArg, design_libraries: Mapping,
                 stock_symbols_dir: _PathArg | None, stock_footprints_dir: _PathArg | None,
                 project_dir: _PathArg | None = None, *,
                 env: Mapping[str, str] | None = None) -> None:
        if design_libraries is None:
            raise TypeError("LibraryResolver(design_libraries=None): pass the netlist's "
                            "'libraries' block ({} when it has none), or use "
                            "LibraryResolver.for_directory(dir, stock_symbols_dir, "
                            "stock_footprints_dir) for a directory without a netlist")
        self.netlist_dir = _abs(netlist_dir)
        self.project_dir = _abs(project_dir) if project_dir is not None else self.netlist_dir
        self.stock_dirs: dict[str, Path | None] = {
            SYMBOL: _abs(stock_symbols_dir) if stock_symbols_dir is not None else None,
            FOOTPRINT: _abs(stock_footprints_dir) if stock_footprints_dir is not None else None}
        self.dir_only = design_libraries is _DIR_ONLY
        self._env = dict(os.environ if env is None else env)
        block = _validate_block(design_libraries)
        self._load_errors: list[str] = []
        self._derived: dict[str, set[str]] = {kind: set() for kind in KINDS}
        self._block = {kind: self._block_decls(kind, block[kind]) for kind in KINDS}
        self._project = {kind: self._project_decls(kind) for kind in KINDS}
        self._conflicts = [msg for kind in KINDS for msg in self._find_conflicts(kind)]

    @classmethod
    def for_directory(cls, directory: _PathArg, stock_symbols_dir: _PathArg | None,
                      stock_footprints_dir: _PathArg | None, *,
                      env: Mapping[str, str] | None = None) -> "LibraryResolver":
        """A resolver for a directory without a netlist (a schematic's, an
        import's): its project tables exactly as KiCad reads them, ``kicad-eda
        managed`` rows included (``dir_only``), then the stock libraries."""
        return cls(directory, _DIR_ONLY, stock_symbols_dir, stock_footprints_dir, env=env)

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
        disabled rows), and each ``symbols`` / ``footprints`` nickname unresolved
        (an iterable of nicknames; a bare str is a TypeError)."""
        _check_nicknames("symbols", symbols)
        _check_nicknames("footprints", footprints)
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
        nickname: fine only for the same library, enabled or disabled alike.
        Equal URI text proves that only for a location-independent URI; a
        relative / ``${KIPRJMOD}`` one resolves against each table's own
        directory, so there the resolved (real) paths decide."""
        clash_path, _ = self._locate(clash.uri, out_dir)
        same_text = clash.uri == decl.uri
        if not ((same_text and _location_independent(decl.uri))
                or _same_file(clash_path, decl.path)):
            hint = (" (the same URI text, but resolved against this table's directory)"
                    if same_text else "")
            problem = f"pointing to {clash_path}{hint}; rename one of them"
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
        Both tables are checked before either is written. Returns a report.
        A bare str for ``symbols`` / ``footprints`` is a TypeError."""
        _check_nicknames("symbols", symbols)
        _check_nicknames("footprints", footprints)
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


def _file_generator(path: Path) -> str | None:
    """The ``(generator ...)`` in one .kicad_sym / .kicad_mod header."""
    try:
        with open(path, "rb") as f:
            head = f.read(HEADER_BYTES)
    except OSError as e:
        raise LibTableError(f"cannot read library file {path}: {e}") from e
    return header_value(head.decode("utf-8", errors="replace"), "generator")


def library_generator(path: _PathArg) -> str | None:
    """KICAD_EDA_GENERATOR (``"kicad-eda"``) when this plugin authored the
    library: a .kicad_sym, or a single .kicad_mod, whose header carries
    ``(generator "kicad-eda")``, or a .pretty in which every .kicad_mod does.
    None for another generator or none, an empty or mixed .pretty, any other
    kind of path or a missing one. An unreadable file raises LibTableError."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == LIB_SUFFIX[FOOTPRINT] and p.is_dir():
        files = sorted(f for f in p.glob("*.kicad_mod") if f.is_file())
    elif suffix in (LIB_SUFFIX[SYMBOL], ".kicad_mod") and p.is_file():
        files = [p]
    else:
        return None
    if files and all(_file_generator(f) == KICAD_EDA_GENERATOR for f in files):
        return KICAD_EDA_GENERATOR
    return None


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
