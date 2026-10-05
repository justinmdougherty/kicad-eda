"""Project libraries, one entry point for every consumer (ROADMAP D1).

``load(netlist, design)`` -> frozen ProjectLibs (block, project tables and
stock libraries via libtable.LibraryResolver.for_design). It never raises:
``.error`` says what is wrong (``resolver`` None = nothing loaded; else the
declared libraries' problems, each naming nickname and path). The rest:
materialize, write_map (swig argv[3]), footprint_digest (route-cache key),
sources / source_callback (review), lint_context, register_in_block, ...
Stdlib only besides toolchain / kicad_env and the stdlib-only server/tools
modules, imported on first use (_tool): importing this module leaves
server/tools off sys.path, as the board modules require. Never mutates inputs.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import kicad_env
import toolchain as tc

if TYPE_CHECKING:                     # server/tools modules: imported lazily by _tool()
    import libtable
    from lint_context import LintContext

PathArg = str | os.PathLike
SourceCallback = Callable[[str, str], "str | None"]

PINS_SUBDIR = "pins"                  # D1: <project>/pins/ holds project-part sidecars
LIBS_SUBDIR = "libs"                  # D1: <netlist dir>/libs/<name>_parts.*
PARTS_SUFFIX = "_parts"
FALLBACK_NAME = "project"
FOOTPRINT_FILE_SUFFIX = ".kicad_mod"
MAP_SUFFIX = ".json"                  # swig_build_board reads argv[3] as a map only if *.json
DIGEST_VERSION = b"kicad-eda footprint digest 1\n"
_NICKNAME_RE = re.compile(r"^[^:\x00-\x1f]+$")     # libtable's / netlist_schema's rule
_SLUG_RE = re.compile(r"[^A-Za-z0-9]*[^A-Za-z0-9_-]+[^A-Za-z0-9]*")   # a run holding a bad char


def _tool(name: str) -> ModuleType:
    """A stdlib-only server/tools module (libtable, review_sources, ...),
    imported on first use with server/tools put on sys.path then."""
    tools = str(tc.TOOLS_DIR)
    if tools not in sys.path:
        sys.path.insert(0, tools)
    return importlib.import_module(name)


@dataclass(frozen=True)
class ProjectLibs:
    """The loaded project libraries (see the module doc). ``stock_*`` are the
    stock directories used ('' = none); ``error`` '' means usable as is."""
    netlist: str
    netlist_dir: Path
    resolver: libtable.LibraryResolver | None = None
    error: str = ""
    stock_symbols: str = ""
    stock_footprints: str = ""

    @property
    def ok(self) -> bool:
        return self.resolver is not None and not self.error


# --- load -------------------------------------------------------------------------------
def _read_design(path: Path) -> dict:
    lt = _tool("libtable")
    try:
        design = json.loads(path.read_text(encoding="utf-8-sig"))     # Notepad's BOM too
    except (OSError, ValueError) as e:
        raise lt.LibTableError(f"cannot read netlist {path}: {e}") from e
    if not isinstance(design, dict):
        raise lt.LibTableError(f"netlist {path} is not a JSON object")
    return design


def load(netlist: PathArg, design: Mapping | None = None, *, symbols_dir: str = "",
         footprints_dir: str = "") -> ProjectLibs:
    """The project libraries of ``netlist`` (its path; ``design`` is the parsed
    netlist, read from the file when None). Stock dirs default to
    toolchain.symbols_dir() / footprints_dir(). Never raises: see ``.error``."""
    if not isinstance(netlist, (str, os.PathLike)) or not os.fspath(netlist):
        return ProjectLibs("", Path.cwd(), error=f"netlist must be the path of a netlist.json, "
                                                 f"got {netlist!r}")
    path = Path(os.path.abspath(os.fspath(netlist)))
    libs = ProjectLibs(str(path), path.parent)
    try:
        syms, fps = tc.symbols_dir(symbols_dir) or "", tc.footprints_dir(footprints_dir) or ""
        libs = replace(libs, stock_symbols=syms, stock_footprints=fps)
        loaded = _read_design(path) if design is None else design
        resolver = _tool("libtable").LibraryResolver.for_design(path.parent, loaded,
                                                                syms or None, fps or None)
        return replace(libs, resolver=resolver, error="; ".join(resolver.errors()))
    except Exception as e:  # noqa: BLE001 - the contract: load never raises, .error says why
        known = isinstance(e, (OSError, ValueError))          # LibTableError is a ValueError
        return replace(libs, error=str(e) if known else f"{type(e).__name__}: {e}")


def _require_resolver(libs: ProjectLibs, what: str) -> libtable.LibraryResolver:
    if libs.resolver is None:
        raise _tool("libtable").LibTableError(f"cannot {what}: the project libraries of "
                                              f"{libs.netlist} did not load: {libs.error}")
    return libs.resolver


def _require_design(design: object) -> Mapping:
    if not isinstance(design, Mapping):
        raise TypeError(f"the design must be the netlist JSON object, got {type(design).__name__}")
    return design


# --- outputs ----------------------------------------------------------------------------
def _atomic_write(path: Path, text: str) -> None:
    """Temp file in the same directory + os.replace: readers see the old file
    or the new one, never half of it; a failure leaves the old file intact.
    The temp is opened like any new file (umask mode, unlike mkstemp's 0600)."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "x", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def materialize(libs: ProjectLibs, design: Mapping, out_dir: PathArg, *,
                kicad_pro: PathArg | None = None) -> list[str]:
    """Merge the declared libraries the design uses (libtable.design_nicknames)
    into ``out_dir``'s tables as ``kicad-eda managed`` rows (resolver.materialize;
    a rerun writes nothing), then a minimal ``kicad_pro`` (relative = in
    ``out_dir``) when given and absent, so kicad-cli reads those tables.
    Returns the paths written. LibTableError: unloaded libraries, a conflict."""
    lt = _tool("libtable")
    resolver = _require_resolver(libs, "materialize library tables")
    report = resolver.materialize(out_dir, **lt.design_nicknames(_require_design(design)))
    written = [report[name]["path"] for name in lt.TABLE_FILES.values()
               if report[name]["written"]]
    if kicad_pro is not None:
        pro = Path(report["out_dir"]) / kicad_pro          # an absolute kicad_pro wins
        if lt.write_minimal_kicad_pro(pro):
            written.append(str(pro))
    return written


def write_map(libs: ProjectLibs, path: PathArg) -> str:
    """Write resolver.library_map() as JSON to ``path`` (must end in .json:
    swig_build_board takes argv[3] as a map only then). Returns the path."""
    resolver = _require_resolver(libs, "write the library map")
    target = Path(path)
    if target.suffix.lower() != MAP_SUFFIX:
        raise ValueError(f"the library map must be a {MAP_SUFFIX} file (swig_build_board reads "
                         f"any other argv[3] as a footprints directory), got {target}")
    _atomic_write(target, json.dumps(resolver.library_map(), indent=2) + "\n")
    return str(target)


# --- footprint digest -------------------------------------------------------------------
def _lib_ref(value: object) -> str | None:
    if not isinstance(value, str) or ":" not in value:
        return None
    nick, name = value.split(":", 1)
    return value if nick and name else None


def footprint_ids(design: object) -> list[str]:
    """Every ``nick:name`` footprint the board build loads, sorted: the
    components' footprints and the mounting-hole footprint (swig_build_board)."""
    if not isinstance(design, Mapping):
        return []
    comps = design.get("components")
    comps = comps if isinstance(comps, list) else []
    found = {_lib_ref(c.get("footprint")) for c in comps if isinstance(c, Mapping)}
    holes = design.get("mounting_holes")
    if holes:
        spec = holes if isinstance(holes, Mapping) else {}
        default = _tool("libtable").DEFAULT_MOUNTING_HOLE_FOOTPRINT
        found.add(_lib_ref(spec.get("footprint", default)))
    return sorted(found - {None})


def _footprint_bytes(resolver: libtable.LibraryResolver | None, lib_id: str) -> bytes | None:
    nick, name = lib_id.split(":", 1)
    hit = resolver.resolve(_tool("libtable").FOOTPRINT, nick) if resolver is not None else None
    if hit is None:
        return None
    try:
        return (hit.path / f"{name}{FOOTPRINT_FILE_SUFFIX}").read_bytes()
    except OSError:
        return None


def footprint_digest(libs: ProjectLibs, design: Mapping) -> str:
    """sha256 (hex) over the sorted (lib_id, .kicad_mod bytes) of every
    footprint the design uses (footprint_ids), stock included: independent of
    paths, component order and the process; an unresolved or unreadable
    footprint hashes as missing (distinct from an empty file). Never raises."""
    digest = hashlib.sha256(DIGEST_VERSION)
    for lib_id in footprint_ids(design):
        data = _footprint_bytes(libs.resolver, lib_id)
        for part in (lib_id.encode("utf-8"), b"-" if data is None else b"+" + data):
            digest.update(len(part).to_bytes(8, "big") + part)     # length-framed
    return digest.hexdigest()


# --- library sources ----------------------------------------------------------------------
def source_callback(libs: ProjectLibs) -> SourceCallback:
    """``resolve(kind, nick)`` for review_report.library_sources_for: stock |
    project | netlist (the block) | generated (libtable.library_generator:
    the whole library is this plugin's) | None (resolves nowhere, its file is
    missing or unreadable, or the libraries did not load)."""
    lt, generated_source = _tool("libtable"), _tool("review_sources").GENERATED
    resolver = libs.resolver
    generated: dict[Path, bool] = {}

    def resolve(kind: str, nick: str) -> str | None:
        hit = resolver.resolve(kind, nick) if resolver is not None else None
        if hit is None or not hit.exists:
            return None
        if hit.path not in generated:
            try:
                author = lt.library_generator(hit.path)
            except lt.LibTableError:
                return None
            generated[hit.path] = author == lt.KICAD_EDA_GENERATOR
        return generated_source if generated[hit.path] else hit.source
    return resolve


def sources(libs: ProjectLibs, design: Mapping) -> dict[str, dict[str, str]]:
    """{ref: {"symbol": src, "footprint": src}}, src stock | project |
    generated | unknown: review_sources.library_sources (per part). When the
    libraries did not load, every library is unknown; when a library file
    cannot be read, the sources are taken per library (source_callback) and
    that library is unknown (lint says why)."""
    design = _require_design(design)
    if libs.resolver is not None:
        try:
            return _tool("review_sources").library_sources(
                design, libs.netlist_dir, libs.stock_symbols or None,
                libs.stock_footprints or None)
        except (_tool("libtable").LibTableError, OSError):
            pass                        # per library below: only the unreadable one is unknown
    return _tool("review_report").library_sources_for(design, source_callback(libs))


# --- paths and names --------------------------------------------------------------------
def pins_dirs(netlist_dir: PathArg) -> list[str]:
    """Pin-sidecar dirs in search order: <netlist_dir>/pins (project parts),
    then the plugin's central PINS_DIR (stock parts)."""
    project = Path(os.path.abspath(os.fspath(netlist_dir))) / PINS_SUBDIR
    central = Path(tc.PINS_DIR)
    if os.path.normcase(project) == os.path.normcase(central):
        return [str(central)]
    return [str(project), str(central)]


def swig_footprint_version_limit() -> int | None:
    """The newest footprint (version N) the SWIG pcbnew (kicad_env.pick_swig)
    loads, by fp_dialect.pcbnew_footprint_limit; None when no install has
    pcbnew, the build is not in fp_dialect's table (a nightly) or the probe fails."""
    try:
        install = kicad_env.pick_swig()
    except Exception:  # noqa: BLE001 - a failed probe means "not known": None is the contract
        return None
    limit = _tool("fp_dialect").pcbnew_footprint_limit
    return None if install is None else limit(install.version)


def _slug(text: object) -> str:
    return _SLUG_RE.sub("_", text).strip("_") if isinstance(text, str) else ""


def default_library_nickname(netlist: PathArg, design: Mapping | None) -> str:
    """``<name>_parts`` (D1): the design's ``name``, else the netlist stem
    (unless 'netlist'), else its directory; a run of characters holding one
    other than letters, digits, '_' and '-' becomes '_' (nickname + file name)."""
    path = Path(os.path.abspath(os.fspath(netlist)))
    stem = path.stem if path.stem.lower() != "netlist" else ""
    name = design.get("name") if isinstance(design, Mapping) else None
    base = next((s for s in map(_slug, (name, stem, path.parent.name)) if s), FALLBACK_NAME)
    return base + PARTS_SUFFIX


def _kind(kind: str) -> str:
    """libtable kind ('symbol' / 'footprint') of a kind or block key."""
    lt = _tool("libtable")
    for k, key in lt.MAP_KEYS.items():
        if kind in (k, key):
            return k
    raise ValueError(f"library kind must be one of {lt.KINDS} (or "
                     f"{tuple(lt.MAP_KEYS.values())}), got {kind!r}")


def default_library_path(kind: str, nick: str) -> str:
    """libs/<nick>.kicad_sym or libs/<nick>.pretty, relative to the netlist."""
    return f"{LIBS_SUBDIR}/{nick}{_tool('libtable').LIB_SUFFIX[_kind(kind)]}"


# --- register_in_block ----------------------------------------------------------------------
def _check_entry(kind: str, nick: object, rel_path: object) -> str:
    if not isinstance(nick, str) or not _NICKNAME_RE.match(nick):
        raise ValueError(f"invalid library nickname {nick!r}: non-empty, without ':' or "
                         f"control characters")
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise ValueError(f"the library path must be a non-empty string, got {rel_path!r}")
    text = rel_path.replace(os.sep, "/")
    suffix = _tool("libtable").LIB_SUFFIX[kind]
    if Path(text).suffix.lower() != suffix:
        raise ValueError(f"a {kind} library path ends in {suffix}, got {rel_path!r}")
    return text


def _location(uri: str, base: Path) -> str:
    text, _missing = _tool("libtable").expand_vars(uri, kiprjmod=base)
    return os.path.normcase(os.path.normpath(base / text))     # an absolute text wins


def _mapping(value: object, what: str) -> Mapping:
    """``value`` as an object (None = empty); LibTableError for anything else."""
    if value is None or isinstance(value, Mapping):
        return value or {}
    raise _tool("libtable").LibTableError(f"{what} must be an object, got {type(value).__name__}")


def _block_entries(design: Mapping, key: str) -> tuple[Mapping, Mapping]:
    block = _mapping(design.get("libraries"), "the netlist 'libraries' block")
    return block, _mapping(block.get(key), f"libraries.{key} (nickname -> path)")


def _check_project_table(base: Path, kind: str, nick: str, location: str) -> None:
    """D1: the block and a project table may not give one nickname two URIs."""
    lt = _tool("libtable")
    table = base / lt.TABLE_FILES[kind]
    if not table.is_file():
        return
    for row in lt.read_table(table).rows:
        if row.name == nick and not row.managed and _location(row.uri, base) != location:
            raise lt.LibTableError(f"{table} already has {kind} library '{nick}' at "
                                   f"{row.uri!r}; the same nickname with another path is a D1 "
                                   f"conflict: pick another nickname")


def register_in_block(netlist: PathArg, kind: str, nick: str, rel_path: str) -> dict:
    """Add ``nick`` -> ``rel_path`` (relative to the netlist dir, ${KIPRJMOD}
    or absolute; stored with '/') to ``libraries.symbols`` / ``.footprints``
    (kind 'symbol' / 'footprint' or the block key) of netlist.json, rewritten
    atomically (temp + os.replace), and return the new design; the same
    location already there is a no-op. LibTableError when the block or an
    unmanaged project-table row has ``nick`` at another path, or the netlist
    is unreadable / malformed; ValueError for bad arguments."""
    kind = _kind(kind)
    text = _check_entry(kind, nick, rel_path)
    path = Path(os.path.abspath(os.fspath(netlist)))
    design = _read_design(path)
    key = _tool("libtable").MAP_KEYS[kind]
    block, entries = _block_entries(design, key)
    location = _location(text, path.parent)
    existing = entries.get(nick)
    if isinstance(existing, str) and _location(existing, path.parent) == location:
        return design
    if existing is not None:
        raise _tool("libtable").LibTableError(f"libraries.{key} already declares '{nick}' as "
                                              f"{existing!r}, not {text!r}: pick another nick")
    _check_project_table(path.parent, kind, nick, location)
    new = {**design, "libraries": {**block, key: {**entries, nick: text}}}
    _atomic_write(path, json.dumps(new, indent=2, ensure_ascii=False) + "\n")
    return new


# --- lint ---------------------------------------------------------------------------------
def lint_context(libs: ProjectLibs, *, probe_swig: bool = True) -> LintContext:
    """The lint run's LintContext: symbols = the resolver when the stock
    symbols are known (else the stock dir; '' skips W2 as before), the
    resolver, library_error = the load error when nothing loaded (LIB rules
    read resolver.errors() themselves), the netlist dir, and
    swig_footprint_version_limit() unless ``probe_swig`` is False."""
    resolver = libs.resolver
    via_resolver = resolver is not None and bool(libs.stock_symbols)
    return _tool("lint_context").LintContext(
        symbols=resolver if via_resolver else libs.stock_symbols, resolver=resolver,
        library_error=libs.error if resolver is None else "", netlist_dir=libs.netlist_dir,
        swig_fp_version_max=swig_footprint_version_limit() if probe_swig else None)
