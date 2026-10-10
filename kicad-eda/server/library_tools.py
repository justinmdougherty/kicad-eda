"""Project-local library tools (F2; docs/ROADMAP_DECISIONS.md D1).

The MCP surface for parts that are not in KiCad's stock libraries:
author_footprint / author_symbol (part presets lg290p, lc29h, opt-in
lg290p_lc29h, fp_spec_json templates, sym_author specs; D0 dialect),
validate_library (KiCad itself loads it), resolve_libraries (the D1 order:
netlist block > project table > stock) and compare_footprints (pad by pad).

Authored parts default to ``<netlist dir>/libs/<name>_parts.pretty`` /
``.kicad_sym`` (nickname ``<name>_parts``). A file is rendered and validated
in a temp dir first (oldest kicad-cli parse check; pcbnew FootprintLoad in
toolchain.kicad_python()) and lands only when KiCad loaded it; a missing
KiCad is a failure, never a pass; a differing file is replaced only with
``overwrite=True``. ``register=True`` adds the library to the netlist's
``libraries`` block (project_libs.register_in_block) and materializes every
library the block declares into the project tables beside the netlist as
``(descr "kicad-eda managed")`` rows: table FILES only, never the
AddLibraryTableEntry RPC or the user's KiCad config.

Every tool returns {"ok", ...} ("reason" when not ok) plus ``assumptions``:
the defaults taken instead of asking, as D5 entries {id, topic, value,
rationale, confidence, override} (decisions.validate_assumption). KiCad
children start only inside fp_author / sym_author (stdin DEVNULL, utf-8 /
replace, a timeout); server/tools modules are imported inside the tools.

Plain functions; kicad_server registers every function in TOOLS with FastMCP
(this module is listed in kicad_server.TOOL_MODULES). A new tool is added by
appending it to TOOLS — kicad_server.py is not edited.
"""

from __future__ import annotations

import contextlib
import importlib
import inspect
import json
import os
import shutil
import sys
import tempfile
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

import project_libs as pl
import toolchain as tc

FOOTPRINT, SYMBOL = "footprint", "symbol"          # libtable's kinds
BLOCK_KEYS = {FOOTPRINT: "footprints", SYMBOL: "symbols"}
LIB_SUFFIX = {FOOTPRINT: ".pretty", SYMBOL: ".kicad_sym"}
FP_SUFFIX = ".kicad_mod"
TEMP_PREFIX = "library_tools_"
NETLIST_SOURCE, STOCK_SOURCE = "netlist", "stock"
_ERRORS = (ValueError, TypeError, OSError, ImportError)


class ToolError(ValueError):
    """A refused request: the tool returns it as {"ok": False, "reason"}, with
    ``extra`` keys and the ``assumptions`` taken before the refusal."""

    def __init__(self, reason: str, *, assumptions: tuple[dict, ...] = (),
                 **extra: Any) -> None:
        super().__init__(reason)
        self.assumptions = tuple(assumptions)
        self.extra = dict(extra)


# --- shared helpers -------------------------------------------------------------------------
def _tool(name: str) -> ModuleType:
    """A server/tools module, imported on first use (server/tools put on sys.path then)."""
    tools = str(tc.TOOLS_DIR)
    if tools not in sys.path:
        sys.path.insert(0, tools)
    return importlib.import_module(name)


@contextlib.contextmanager
def _symbols() -> Iterator[ModuleType]:
    """sym_author, its SymbolAuthorError turned into a ToolError."""
    sa = _tool("sym_author")
    try:
        yield sa
    except sa.SymbolAuthorError as e:
        raise ToolError(str(e)) from e


def _guarded(body: Callable[..., dict], *args: Any) -> dict:
    """body(*args); a refusal or a bad input becomes {"ok": False, "reason", ...}."""
    try:
        return body(*args)
    except _ERRORS as exc:
        reason = str(exc) or type(exc).__name__
        if isinstance(exc, ImportError):
            reason = (f"a Python dependency is missing ({exc}); install "
                      f"server/requirements.txt into the server's Python")
        return {**getattr(exc, "extra", {}), "ok": False, "reason": reason,
                "assumptions": list(getattr(exc, "assumptions", ()))}


def _envelope(issues: list[str], assumptions: tuple[dict, ...], **data: Any) -> dict:
    """{"ok": no issues, "reason" (when not ok), **data, "assumptions"}."""
    head: dict = {"ok": not issues}
    if issues:
        more = f" (+{len(issues) - 3} more)" if len(issues) > 3 else ""
        head["reason"] = f"{len(issues)} problem(s): " + "; ".join(issues[:3]) + more
    return {**head, **data, "assumptions": list(assumptions)}


def _assume(aid: str, topic: str, value: Any, rationale: str, confidence: str = "high",
            override: str | None = None) -> dict:
    """A D5 assumption entry (decisions.validate_assumption)."""
    return {"id": aid, "topic": topic, "value": value, "rationale": rationale,
            "confidence": confidence, "override": override}


def _netlist(netlist: Any) -> tuple[Path, dict]:
    if not isinstance(netlist, str) or not netlist.strip():
        raise ToolError(f"netlist must be the path of the design's netlist.json, got {netlist!r}")
    path = Path(os.path.abspath(netlist.strip()))
    if not path.is_file():
        raise ToolError(f"netlist not found: {path}")
    try:
        design = json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeError, ValueError) as e:
        raise ToolError(f"{path} is not a JSON netlist: {e}") from e
    if not isinstance(design, dict):
        raise ToolError(f"{path} is not a JSON object netlist (got {type(design).__name__})")
    return path, design


def _libs(path: Path, design: Mapping | None = None) -> pl.ProjectLibs:
    libs = pl.load(path, design)
    if libs.resolver is None:
        raise ToolError(f"the project libraries of {path} did not load: {libs.error}")
    return libs


def _params(value: Any, what: str = "params") -> dict:
    """A JSON object argument (a mapping, or its JSON text; None / '' = {}), copied."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return {}
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except ValueError as e:
        raise ToolError(f"{what} is not valid JSON: {e}") from e
    if not isinstance(value, Mapping):
        raise ToolError(f"{what} must be a JSON object (or its text), got "
                        f"{type(value).__name__}")
    return dict(value)


def _write(path: Path, text: str) -> None:
    """Temp file beside ``path`` + os.replace: never half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "x", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _kicad() -> tuple[str | None, str, str | None]:
    """(oldest kicad-cli, its label, toolchain.kicad_python()): the D0 dialect
    floor parses the file, the SWIG builder's pcbnew loads it."""
    cli, label = _tool("fp_author").oldest_kicad_cli()
    return cli, label, tc.kicad_python()


# --- library targets ------------------------------------------------------------------------
@dataclass(frozen=True)
class _Target:
    """Where an authored part goes: ``path`` is the .pretty dir / .kicad_sym
    file, ``entry`` its libraries-block path (relative to the netlist dir)."""
    kind: str
    nick: str
    path: Path
    entry: str


def _check_nick(nick: str) -> str:
    if not nick or ":" in nick or any(ord(c) < 32 for c in nick):
        raise ToolError(f"invalid library nickname {nick!r}: non-empty, without ':' or "
                        f"control characters")
    return nick


def _relative(path: Path, base: Path) -> str:
    """``path`` relative to ``base`` with '/', or absolute on another drive."""
    try:
        return Path(os.path.relpath(path, base)).as_posix()
    except ValueError:
        return path.as_posix()


def _declared(libs: pl.ProjectLibs, kind: str, nick: str) -> Any:
    """The resolver's hit for ``nick``; a stock library is never written into."""
    hit = libs.resolver.resolve(kind, nick)
    if hit is not None and hit.source == STOCK_SOURCE:
        raise ToolError(f"'{nick}' is a stock KiCad {kind} library ({hit.path}); authored "
                        f"parts go into a project library (D1): pass another library")
    return hit


def _path_target(kind: str, base: Path, libs: pl.ProjectLibs, text: str) -> _Target:
    suffix = LIB_SUFFIX[kind]
    given = Path(text)
    if given.suffix.lower() != suffix:
        raise ToolError(f"a {kind} library path ends in {suffix}, got {text!r}")
    full = Path(os.path.normpath(given if given.is_absolute() else base / given))
    nick = _check_nick(full.stem)
    hit = _declared(libs, kind, nick)
    if hit is None:
        return _Target(kind, nick, full, _relative(full, base))
    if os.path.normcase(hit.path) != os.path.normcase(full):
        raise ToolError(f"{kind} library '{nick}' is already declared at {hit.path} "
                        f"({hit.origin}); the same nickname at {full} would be a D1 conflict: "
                        f"rename the file or pass library='{nick}'")
    return _Target(kind, nick, full, hit.uri if hit.source == NETLIST_SOURCE
                   else _relative(full, base))


def _target(kind: str, netlist: Path, design: Mapping, libs: pl.ProjectLibs,
            library: Any) -> tuple[_Target, tuple[dict, ...]]:
    """The library ``library`` names: '' = the D1 default ``<name>_parts``, a
    nickname (its declaration when the block or a project table has one,
    else libs/<nick>.*), or a path ending in .pretty / .kicad_sym."""
    if not isinstance(library, str):
        raise ToolError(f"library must be a nickname or a library path, got {library!r}")
    text, base = library.strip(), netlist.parent
    if text.lower().endswith((*LIB_SUFFIX.values(), FP_SUFFIX)) or "/" in text or "\\" in text:
        return _path_target(kind, base, libs, text), ()
    nick = _check_nick(text or pl.default_library_nickname(netlist, design))
    hit = _declared(libs, kind, nick)
    if hit is not None:
        entry = hit.uri if hit.source == NETLIST_SOURCE else _relative(hit.path, base)
        target = _Target(kind, nick, hit.path, entry)
    else:
        entry = pl.default_library_path(kind, nick)
        target = _Target(kind, nick, Path(os.path.normpath(base / entry)), entry)
    if text:
        return target, ()
    return target, (_assume(
        f"author_{kind}.library", f"{kind} library", f"{nick} -> {target.entry}",
        "D1 default for authored parts: <netlist dir>/libs/<name>_parts.*, nickname "
        "<name>_parts (the netlist's name, else its file stem or folder)",
        override=f"libraries.{BLOCK_KEYS[kind]}.{nick}"),)


# --- registration and tables ----------------------------------------------------------------
def _materialize(netlist: Path) -> dict:
    """Every library the netlist's block declares, merged into the project
    tables beside it as managed rows (a rerun writes nothing)."""
    libs = _libs(netlist)
    report = libs.resolver.materialize(netlist.parent)
    tables = {name: report[name] for name in _tool("libtable").TABLE_FILES.values()}
    return {"written": [t["path"] for t in tables.values() if t["written"]],
            "tables": {name: t["path"] for name, t in tables.items()},
            "managed": {name: t["managed"] for name, t in tables.items()}}


def _register(netlist: Path, target: _Target, register: bool,
              lib_id: str) -> tuple[dict, tuple[dict, ...]]:
    key = BLOCK_KEYS[target.kind]
    if not register:
        return {"registered": False, "block_entry": None, "materialized": None}, (_assume(
            f"author_{target.kind}.register", "library registration", "not registered",
            f"register=False: {lib_id} resolves only once '{target.nick}' is declared in the "
            f"netlist libraries block or a project table", "high", f"libraries.{key}"),)
    pl.register_in_block(netlist, target.kind, target.nick, target.entry)
    return {"registered": True, "block_entry": {key: {target.nick: target.entry}},
            "materialized": _materialize(netlist)}, ()


# --- author_footprint -----------------------------------------------------------------------
def _template_spec(template: str, data: dict, name: str) -> Any:
    fsj = _tool("fp_spec_json")
    keys = fsj.template_keys(template)
    takes_name = "name" in keys["required"] + keys["optional"]
    if name and takes_name:
        if "name" in data and data["name"] != name:
            raise ToolError(f"name={name!r} differs from params.name={data['name']!r}: give "
                            f"the footprint name once")
        data = {**data, "name": name}
    spec = fsj.spec_from_json(template, data)
    return replace(spec, name=name) if name and not takes_name else spec


def _preset_spec(preset: str, data: dict, name: str, opt_in: bool) -> tuple[Any, dict, dict]:
    """(spec, preset result keys, the hints assumption) of a part preset; params
    may hold only the preset's own keyword options (e.g. ``stencil``)."""
    pp = _tool("part_presets")
    found = pp.get_preset(preset, opt_in=opt_in)
    sig = inspect.signature(found.footprint_fn).parameters.values()
    allowed = sorted(p.name for p in sig if p.kind is inspect.Parameter.KEYWORD_ONLY)
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ToolError(f"preset {preset!r} takes params {allowed or 'none'}, got {unknown}")
    spec = found.footprint_fn(**data)
    spec = replace(spec, name=name) if name else spec
    with _symbols() as sa:
        groups = sa.pin_groups(found.symbol_spec_fn())
    hints = pp.preset_hints(preset, opt_in=opt_in)
    extra = {"preset": preset, "netlist_hints": hints, "strategy": pp.PRESET_STRATEGY[preset],
             "pin_groups": groups}
    note = _assume("author_footprint.netlist_hints", "LGA inner GND pads",
                   f"{len(hints.get('preroutes', []))} preroutes (footprint frame, part U1)",
                   f"{pp.PRESET_STRATEGY[preset]}. Place them at the part with "
                   f"lga_strategy.apply_hints(design, netlist_hints, ref=<ref>)",
                   "high", "preroutes")
    return spec, extra, note


def _footprint_spec(template: Any, preset: Any, params: Any, name: Any,
                    opt_in: bool) -> tuple[Any, dict, tuple[dict, ...]]:
    if not isinstance(name, str):
        raise ToolError(f"name must be a footprint name, got {name!r}")
    if not isinstance(template, str) or not isinstance(preset, str) \
            or bool(template.strip()) == bool(preset.strip()):
        raise ToolError("pass exactly one of template= (fp_spec_json: lga_grid, qfn, dfn, "
                        "lcc_castellated, sop, pin_header, lg290p) or preset= (part_presets: "
                        "lg290p, lc29h, opt-in lg290p_lc29h)")
    data = _params(params)
    if preset.strip():
        spec, extra, note = _preset_spec(preset.strip(), data, name.strip(), opt_in)
        return spec, extra, (note,)
    return _template_spec(template.strip(), data, name.strip()), {}, ()


def _state(dest: Path, text: str, overwrite: bool, differs: Callable[[], str]) -> str:
    """'new' | 'unchanged' | 'replaced'; ToolError for a differing file without overwrite."""
    if not dest.exists():
        return "new"
    if not dest.is_file():
        raise ToolError(f"{dest} exists and is not a file")
    if dest.read_text(encoding="utf-8", errors="replace") == text:
        return "unchanged"
    if not overwrite:
        raise ToolError(f"{dest} already exists and differs ({differs()}); pass "
                        f"overwrite=True to replace it")
    return "replaced"


def _pad_diff(old: Path, new: Path) -> str:
    try:
        return _tool("fp_compare").compare_footprints(old, new)["summary"]
    except ValueError as e:                     # the existing file does not parse
        return f"existing file unreadable: {e}"


def _load_problems(load: Mapping | None, pads: int) -> list[str]:
    """pcbnew FootprintLoad must have run and loaded every pad entry."""
    if load is None:
        return ["pcbnew FootprintLoad did not run: " + (
            tc.kicad_python_error() or "no KiCad bundled Python with pcbnew found")]
    if not load.get("ok"):
        return [f"pcbnew FootprintLoad failed: {load.get('error') or 'not loaded'}"]
    if load.get("pads") != pads:
        return [f"pcbnew loaded {load.get('pads')} pads, the file has {pads}"]
    return []


def _check_footprint(file: Path, expected: int | None, cli: str | None,
                     python: str | None) -> dict:
    """fp_author.validate (static checks + the kicad-cli parse check) and
    pcbnew FootprintLoad (skipped for a file that does not even parse)."""
    fa = _tool("fp_author")
    problems = list(fa.validate(file, expected_pads=expected, kicad_cli=cli))
    try:
        pads = fa.read_footprint(file).pads
    except ValueError:                           # validate() already said why
        pads = ()
    load = fa.pcbnew_load(file, python=python) if python and pads else None
    problems += _load_problems(load, len(pads)) if pads else []
    view = None if load is None else {k: load[k] for k in (
        "ok", "loaded", "pads", "courtyard_outlines", "kicad_version", "error") if k in load}
    numbered = {p.number for p in pads if p.number and fa.has_copper(p)}
    return {"name": file.stem, "path": str(file), "pads": len(numbered),
            "pad_entries": len(pads), "problems": problems, "kicad_cli": cli,
            "kicad_python": python, "pcbnew": view}


def _stage_footprint(spec: Any, target: _Target, overwrite: bool) -> tuple[str, str, dict]:
    """(text, state, validation): rendered and validated in a temp .pretty."""
    fa = _tool("fp_author")
    text = fa.render(spec)
    dest = target.path / f"{spec.name}{FP_SUFFIX}"
    cli, _label, python = _kicad()
    with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX) as tmp:
        staged = Path(tmp) / f"{target.nick}{LIB_SUFFIX[FOOTPRINT]}" / dest.name
        staged.parent.mkdir()
        staged.write_text(text, encoding="utf-8", newline="\n")
        state = _state(dest, text, overwrite, lambda: _pad_diff(dest, staged))
        check = _check_footprint(staged, fa.summary(spec)["pads"], cli, python)
    return text, state, {**check, "path": str(dest)}


def _author_footprint(netlist: Any, template: Any, preset: Any, params: Any, name: Any,
                      library: Any, register: bool, overwrite: bool, opt_in: bool) -> dict:
    path, design = _netlist(netlist)
    spec, extra, notes = _footprint_spec(template, preset, params, name, opt_in)
    target, lib_notes = _target(FOOTPRINT, path, design, _libs(path, design), library)
    notes += lib_notes
    lib_id = f"{target.nick}:{spec.name}"
    text, state, check = _stage_footprint(spec, target, overwrite)
    if check["problems"]:
        raise ToolError(f"{lib_id} failed validation, nothing written: "
                        + "; ".join(check["problems"]), assumptions=notes, validation=check)
    dest = target.path / f"{spec.name}{FP_SUFFIX}"
    if state != "unchanged":
        _write(dest, text)
    reg, reg_notes = _register(path, target, register, lib_id)
    summary = _tool("fp_author").summary(spec)
    return {"ok": True, "lib_id": lib_id, "footprint": spec.name, "nickname": target.nick,
            "library": str(target.path), "path": str(dest), "state": state,
            "written": state != "unchanged", "pads": summary["pads"],
            "pad_numbers": summary["pad_numbers"], "courtyard": summary["courtyard"],
            "validation": check, **reg, **extra, "assumptions": list(notes + reg_notes)}


def author_footprint(netlist: str, template: str = "", preset: str = "",
                     params: dict[str, Any] | str | None = None, name: str = "",
                     library: str = "", register: bool = True, overwrite: bool = False,
                     opt_in: bool = False) -> dict:
    """Author a KiCad footprint (D0 dialect, version 20241229) into a project
    library. netlist: the design's netlist.json path. Give exactly one of
    preset (lg290p | lc29h; lg290p_lc29h needs opt_in=True and takes params
    {"stencil": "lg290p"|"lc29h"}) or template (lga_grid | qfn | dfn |
    lcc_castellated | sop | pin_header | lg290p) with params, the template's
    keyword arguments as a JSON object (fp_spec_json). name: the footprint
    name (else the template's / preset's). library: '' = <netlist
    dir>/libs/<name>_parts.pretty (nickname <name>_parts), a nickname, or a
    .pretty path relative to the netlist. Validated before it is written
    (oldest kicad-cli parse check + pcbnew FootprintLoad); a different
    existing file is replaced only with overwrite=True. register: add the
    library to the netlist libraries block and materialize fp-lib-table.
    Returns {ok, lib_id, path, pads, validation, state: new|unchanged|
    replaced, registered, materialized, assumptions}; a preset also returns
    netlist_hints (LGA inner-pad escape vias, F2-S1 strategy f, footprint
    frame: apply with lga_strategy.apply_hints), strategy and pin_groups."""
    return _guarded(_author_footprint, netlist, template, preset, params, name, library,
                    register, overwrite, opt_in)


# --- author_symbol --------------------------------------------------------------------------
def _footprint_holder(netlist: Path, design: Mapping, libs: pl.ProjectLibs,
                      fp_name: str) -> tuple[str, dict]:
    """'<nick>:<fp_name>' of the project library holding the preset's
    footprint (the D1 default library first), and the assumption made."""
    default = pl.default_library_nickname(netlist, design)
    declared = libs.resolver.library_map()[BLOCK_KEYS[FOOTPRINT]]
    lib_id, why, confidence = (f"{default}:{fp_name}", (
        f"the preset's footprint in the D1 default library, not authored yet: call "
        f"author_footprint(netlist, preset=...) so {default}:{fp_name} resolves"), "med")
    for nick in [default, *sorted(n for n in declared if n != default)]:
        hit = libs.resolver.resolve(FOOTPRINT, nick)
        if hit is not None and hit.source != STOCK_SOURCE \
                and (hit.path / f"{fp_name}{FP_SUFFIX}").is_file():
            lib_id, confidence = f"{nick}:{fp_name}", "high"
            why = f"the preset's footprint, authored in project library '{nick}' ({hit.path})"
            break
    return lib_id, _assume("author_symbol.footprint", "Footprint property", lib_id, why,
                           confidence, "footprint")


def _symbol_spec(spec: Any, preset: Any, footprint: Any, opt_in: bool, netlist: Path,
                 design: Mapping, libs: pl.ProjectLibs) -> tuple[Any, tuple[dict, ...]]:
    has_spec = spec is not None and spec != "" and spec != {}
    if not isinstance(preset, str) or has_spec == bool(preset.strip()):
        raise ToolError("pass exactly one of spec= (a sym_author spec: {name, pins: [{number, "
                        "name, type, side?, group?}], reference?, footprint?, ...}) or preset= "
                        "(part_presets: lg290p, lc29h, opt-in lg290p_lc29h)")
    if not isinstance(footprint, str) or (footprint.strip() and ":" not in footprint):
        raise ToolError(f"footprint must be a 'nick:name' lib_id (or ''), got {footprint!r}")
    footprint = footprint.strip()
    if preset.strip():
        found = _tool("part_presets").get_preset(preset.strip(), opt_in=opt_in)
        notes: tuple[dict, ...] = ()
        if not footprint:
            footprint, note = _footprint_holder(netlist, design, libs,
                                                found.footprint_fn().name)
            notes = (note,)
        return found.symbol_spec_fn(footprint=footprint), notes
    with _symbols() as sa:
        sym = sa.spec_from_dict(_params(spec, "spec"))
    sym = replace(sym, footprint=footprint) if footprint else sym
    if sym.footprint:
        return sym, ()
    return sym, (_assume("author_symbol.footprint", "Footprint property", "",
                         "neither the spec nor footprint= names a footprint: every component "
                         "using this symbol must set 'footprint' in the netlist", "med",
                         "components[*].footprint"),)


def _check_counts(counts: Mapping[str, int], expected: Any, what: str) -> list[str]:
    """Each entry (or each named one) against expected_pads: an int or {name: int}."""
    if expected is None:
        return []
    if isinstance(expected, Mapping):
        return [f"{name}: not in the library" if name not in counts else
                f"{name}: {counts[name]} {what}, expected {n}"
                for name, n in expected.items() if counts.get(name) != n]
    return [f"{name}: {n} {what}, expected {expected}"
            for name, n in counts.items() if n != expected]


def _check_symbols(lib: Path, expected: Any, cli: str | None) -> dict:
    """sym_author.validate_library (symlib flatten + kicad-cli sym upgrade)."""
    with _symbols() as sa:
        res = sa.validate_library(lib, cli)
    pins, hidden = res["symlib_pins"], res["symlib_hidden_pins"]
    return {"symbols": {n: {"pins": c, "hidden_pins": hidden.get(n, 0)} for n, c in pins.items()},
            "problems": [*res["errors"], *_check_counts(pins, expected, "pins")],
            "kicad_cli": cli}


def _stage_symbol(sym: Any, target: _Target, overwrite: bool) -> tuple[str, dict, str, dict]:
    """(library text, sym_author summary, state, validation), all in a temp copy."""
    cli = _kicad()[0]
    with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX) as tmp, _symbols() as sa:
        staged = Path(tmp) / target.path.name
        if target.path.is_file():                # sym_author adds to / replaces in a copy
            shutil.copyfile(target.path, staged)
        summary = sa.author_symbol(sym, staged)
        text = staged.read_text(encoding="utf-8")
        state = _state(target.path, text, overwrite or not summary["replaced"],
                       lambda: f"symbol {sym.name!r} is already in it")
        if state == "replaced" and not summary["replaced"]:
            state = "added"
        check = _check_symbols(staged, {sym.name: len(sym.pins)}, cli)
    return text, summary, state, {**check, "path": str(target.path)}


def _sidecar(lib: Path, symbol: str, nick: str, netlist: Path, wanted: bool) -> dict | None:
    if not wanted:
        return None
    with _symbols() as sa:
        return sa.write_pin_sidecar(lib, symbol, netlist.parent / pl.PINS_SUBDIR, nickname=nick)


def _author_symbol(netlist: Any, spec: Any, preset: Any, library: Any, footprint: Any,
                   register: bool, pins_sidecar: bool, overwrite: bool, opt_in: bool) -> dict:
    path, design = _netlist(netlist)
    libs = _libs(path, design)
    sym, notes = _symbol_spec(spec, preset, footprint, opt_in, path, design, libs)
    target, lib_notes = _target(SYMBOL, path, design, libs, library)
    notes += lib_notes
    lib_id = f"{target.nick}:{sym.name}"
    text, summary, state, check = _stage_symbol(sym, target, overwrite)
    if check["problems"]:
        raise ToolError(f"{lib_id} failed validation, nothing written: "
                        + "; ".join(check["problems"]), assumptions=notes, validation=check)
    if state != "unchanged":
        _write(target.path, text)
    sidecar = _sidecar(target.path, sym.name, target.nick, path, pins_sidecar)
    reg, reg_notes = _register(path, target, register, lib_id)
    keep = ("pins", "visible_pins", "stacked", "pin_groups", "body_mm", "format_version")
    return {"ok": True, "lib_id": lib_id, "symbol": sym.name, "nickname": target.nick,
            "library": str(target.path), "state": state, "written": state != "unchanged",
            "footprint": sym.footprint, **{k: summary[k] for k in keep},
            "validation": check, "pins_sidecar": sidecar, **reg,
            **({"preset": preset.strip()} if isinstance(preset, str) and preset.strip() else {}),
            "assumptions": list(notes + reg_notes)}


def author_symbol(netlist: str, spec: dict[str, Any] | str | None = None, preset: str = "",
                  library: str = "", footprint: str = "", register: bool = True,
                  pins_sidecar: bool = True, overwrite: bool = False,
                  opt_in: bool = False) -> dict:
    """Author a KiCad symbol (D0 dialect, version 20241209) into a project
    library: one pin per pad (pad number = pin number), power pins sharing a
    name and type stacked on the first (hidden, passive). netlist: the
    design's netlist.json path. Give exactly one of preset (lg290p | lc29h;
    lg290p_lc29h needs opt_in=True) or spec, a JSON object {name, pins:
    [{number, name, type, side?, group?}], reference?, value?, footprint?,
    datasheet?, description?, keywords?, fp_filters?, stack_power?}.
    footprint: the Footprint property 'nick:name'; a preset defaults it to
    its footprint in the project library holding it (D1 default first).
    library: '' = <netlist dir>/libs/<name>_parts.kicad_sym, a nickname, or a
    .kicad_sym path. Validated before it is written (oldest kicad-cli sym
    upgrade); a different same-name symbol is replaced only with
    overwrite=True. pins_sidecar: write <netlist dir>/pins/<nick>_<symbol>.
    pins.json. register: add the library to the netlist libraries block and
    materialize sym-lib-table. Returns {ok, lib_id, footprint, pins,
    pin_groups (e.g. GND -> the 59 pad numbers to put on one net), stacked,
    state: new|added|unchanged|replaced, validation, pins_sidecar,
    registered, materialized, assumptions}."""
    return _guarded(_author_symbol, netlist, spec, preset, library, footprint, register,
                    pins_sidecar, overwrite, opt_in)


# --- validate_library -----------------------------------------------------------------------
def _expected(value: Any) -> Any:
    ok_int = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    ok_map = isinstance(value, Mapping) and all(
        isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in value.values())
    if value is None or ok_int or ok_map:
        return value
    raise ToolError(f"expected_pads must be a count >= 0 or {{name: count}}, got {value!r}")


def _footprint_files(lib: Path) -> list[Path]:
    if lib.suffix.lower() == FP_SUFFIX and lib.is_file():
        return [lib]
    if lib.suffix.lower() == LIB_SUFFIX[FOOTPRINT] and lib.is_dir():
        files = sorted(f for f in lib.glob(f"*{FP_SUFFIX}") if f.is_file())
        if not files:
            raise ToolError(f"{lib} holds no .kicad_mod footprints")
        return files
    raise ToolError(f"{lib} is not a footprint library (.pretty), a footprint (.kicad_mod) or "
                    f"a symbol library (.kicad_sym)")


def _validate_library(path: Any, expected_pads: Any) -> dict:
    if not isinstance(path, str) or not path.strip():
        raise ToolError(f"path must be a .pretty, .kicad_mod or .kicad_sym path, got {path!r}")
    lib = Path(os.path.abspath(path.strip()))
    if not lib.exists():
        raise ToolError(f"library not found: {lib}")
    expected = _expected(expected_pads)
    cli, label, python = _kicad()
    note = (_assume("validate_library.kicad", "validating KiCad", label or "none",
                    "D0: the oldest installed kicad-cli is the dialect floor every consumer "
                    "reads; footprints are also loaded by the SWIG builder's pcbnew"),)
    if lib.suffix.lower() == LIB_SUFFIX[SYMBOL] and lib.is_file():
        res = _check_symbols(lib, expected, cli)
        return _envelope(res["problems"], note, kind=SYMBOL, path=str(lib),
                         pins={n: s["pins"] for n, s in res["symbols"].items()},
                         symbols=res["symbols"], problems=res["problems"], kicad_cli=cli)
    checks = {f.stem: _check_footprint(f, expected if not isinstance(expected, Mapping)
                                       else expected.get(f.stem), cli, python)
              for f in _footprint_files(lib)}
    pads = {n: c["pads"] for n, c in checks.items()}
    missing = [f"{n}: not in the library" for n in (expected if isinstance(expected, Mapping)
                                                    else {}) if n not in checks]
    problems = [f"{n}: {p}" for n, c in checks.items() for p in c["problems"]] + missing
    return _envelope(problems, note, kind=FOOTPRINT, path=str(lib), pads=pads,
                     footprints=checks, problems=problems, kicad_cli=cli, kicad_python=python)


def validate_library(path: str, expected_pads: int | dict[str, int] | None = None) -> dict:
    """Validate a project library by loading it with KiCad itself (D1): a
    footprint library (.pretty: every .kicad_mod in it), one footprint
    (.kicad_mod), or a symbol library (.kicad_sym). Footprints: dialect,
    courtyard, silk and copper-overlap checks, the oldest kicad-cli's `fp
    upgrade` parse check (KiCad must re-serialise the same pads) and pcbnew
    FootprintLoad in the KiCad Python the board builder uses. Symbols: every
    symbol flattened, `sym upgrade` must keep every pin. expected_pads: a pad
    (symbol: pin) count every entry must have, or {name: count}. A missing
    KiCad is a problem, never a pass. Returns {ok, kind: footprint|symbol,
    pads {name: n} / pins {name: n}, footprints / symbols (per entry
    problems, pcbnew load), problems, kicad_cli, assumptions}."""
    return _guarded(_validate_library, path, expected_pads)


# --- resolve_libraries ----------------------------------------------------------------------
def _generated(path: Path) -> bool:
    lt = _tool("libtable")
    try:
        return lt.library_generator(path) == lt.KICAD_EDA_GENERATOR
    except lt.LibTableError:
        return False


def _nick_info(libs: pl.ProjectLibs, kind: str, nick: str, used: bool,
               errors: list[str]) -> dict:
    mine = [e for e in errors if e.startswith(f"{kind} library '{nick}'")]
    hit = libs.resolver.resolve(kind, nick)
    if hit is None:
        return {"source": None, "path": None, "exists": False, "used": used, "errors": mine}
    return {"source": hit.source, "path": str(hit.path), "exists": hit.exists, "uri": hit.uri,
            "origin": hit.origin, "used": used, "errors": mine,
            "generated": hit.source != STOCK_SOURCE and hit.exists and _generated(hit.path)}


def _try_materialize(netlist: Path) -> tuple[dict, list[str]]:
    try:
        return _materialize(netlist), []
    except ValueError as e:                      # LibTableError: a D1 conflict
        return {"error": str(e)}, [f"materialize: {e}"]


def _resolve_libraries(netlist: Any, materialize: bool) -> dict:
    path, design = _netlist(netlist)
    libs = _libs(path, design)
    lt = _tool("libtable")
    used = lt.design_nicknames(design)
    declared = libs.resolver.library_map()
    errors = libs.resolver.errors(**used)
    out = {key: {nick: _nick_info(libs, kind, nick, nick in used[key], errors)
                 for nick in sorted(set(used[key]) | set(declared[key]))}
           for kind, key in lt.MAP_KEYS.items()}
    made, made_problems = _try_materialize(path) if materialize else (None, [])
    return _envelope([*errors, *made_problems], (), netlist=str(path),
                     netlist_dir=str(path.parent), **out, errors=errors,
                     stock={"symbols": libs.stock_symbols or None,
                            "footprints": libs.stock_footprints or None},
                     materialized=made)


def resolve_libraries(netlist: str, materialize: bool = False) -> dict:
    """Resolve every library nickname of a netlist in the D1 order: the
    netlist 'libraries' block, then the project sym-lib-table / fp-lib-table
    beside it, then the stock libraries of the install the builders use.
    Covers the nicknames the design uses (components' lib_id / footprint,
    power, the mounting-hole footprint) and every declared one. Returns {ok
    (no library problem), symbols / footprints: {nick: {source:
    netlist|project|stock|None, path, exists, uri, origin, used, generated
    (authored by kicad-eda), errors}}, errors, stock, materialized,
    assumptions}. materialize=True also merges every library the block
    declares into the project tables beside the netlist as kicad-eda managed
    rows (unmanaged rows are never touched; a rerun writes nothing)."""
    return _guarded(_resolve_libraries, netlist, materialize)


# --- compare_footprints ---------------------------------------------------------------------
def _footprint_library(nick: str, libs: pl.ProjectLibs | None) -> tuple[Path, str]:
    if libs is not None:
        hit = libs.resolver.resolve(FOOTPRINT, nick)
        if hit is None:
            raise ToolError(f"footprint library '{nick}' not found (netlist libraries block, "
                            f"project fp-lib-table, stock libraries)")
        return hit.path, hit.source
    stock = tc.footprints_dir()
    lib = Path(stock) / f"{nick}{LIB_SUFFIX[FOOTPRINT]}" if stock else None
    if lib is None or not lib.is_dir():
        raise ToolError(f"footprint library '{nick}' is not a stock library ({lib}); pass "
                        f"netlist= to resolve project libraries")
    return lib, STOCK_SOURCE


def _footprint_file(ref: Any, base: Path | None,
                    libs: pl.ProjectLibs | None) -> tuple[Path, dict]:
    """A .kicad_mod path (relative to ``base``), or a lib_id resolved through
    ``libs`` (stock only when None), and where it came from."""
    if not isinstance(ref, str) or not ref.strip():
        raise ToolError(f"a footprint must be a 'nick:name' lib_id or a .kicad_mod path, "
                        f"got {ref!r}")
    text = ref.strip()
    if text.lower().endswith(FP_SUFFIX):
        given = Path(text)
        path = given if given.is_absolute() or base is None else base / given
        if not path.is_file():
            raise ToolError(f"footprint file not found: {path}")
        return path, {"kind": "path", "path": str(path)}
    nick, _sep, name = text.partition(":")
    if not (nick and name):
        raise ToolError(f"{ref!r} is neither a 'nick:name' lib_id nor a .kicad_mod path")
    lib, source = _footprint_library(nick, libs)
    path = lib / f"{name}{FP_SUFFIX}"
    if not path.is_file():
        raise ToolError(f"{text}: {path} does not exist")
    return path, {"kind": "lib_id", "lib_id": text, "source": source, "path": str(path)}


def _compare(a: Any, b: Any, netlist: Any, tolerance: Any) -> dict:
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) \
            or not 0 <= tolerance < float("inf"):
        raise ToolError(f"tolerance must be a distance >= 0 (mm), got {tolerance!r}")
    libs, base = None, None
    if netlist:
        path, design = _netlist(netlist)
        libs, base = _libs(path, design), path.parent
    file_a, src_a = _footprint_file(a, base, libs)
    file_b, src_b = _footprint_file(b, base, libs)
    stock_only = libs is None and "lib_id" in (src_a["kind"], src_b["kind"])
    notes = (_assume("compare_footprints.stock_only", "library resolution", "stock only",
                     "no netlist given: lib_ids resolve against the stock footprint libraries",
                     "high", "netlist"),) if stock_only else ()
    diff = _tool("fp_compare").compare_footprints(file_a, file_b, tolerance=float(tolerance))
    return {"ok": True, **diff, "sources": {"a": src_a, "b": src_b},
            "assumptions": list(notes)}


def compare_footprints(a: str, b: str, netlist: str = "", tolerance: float = 0.001) -> dict:
    """Compare two footprints pad by pad: a and b are 'nick:name' lib_ids
    (resolved through netlist's libraries block, project fp-lib-table and the
    stock libraries; stock only without a netlist) or .kicad_mod paths
    (relative to the netlist's folder when one is given). Pads pair by
    position (nearest first within 1 mm; copper and paste / mask apertures
    separately). Returns {ok, identical, summary, pads [a, b], matched,
    max_offset, deviations (moved / resized / shape / type / layers /
    renumbered, per pad), numbering (duplicates, numbers only one side has),
    unmatched_a / unmatched_b, apertures, sources, assumptions}; tolerance in
    mm."""
    return _guarded(_compare, a, b, netlist, tolerance)


TOOLS: tuple[Callable[..., Any], ...] = (author_footprint, author_symbol, validate_library,
                                         resolve_libraries, compare_footprints)
