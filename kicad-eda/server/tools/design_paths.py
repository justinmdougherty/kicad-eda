#!/usr/bin/env python3
"""Shared helpers of the design transforms (rebuild_variant, design_blocks):
the path rules of a netlist.json design (ROADMAP_DECISIONS D1), footprint
lookup and the shapes of the records the transforms read.

Every file a design names is relative to the netlist.json directory,
``${KIPRJMOD}``-prefixed (= that directory) or absolute. A transform that
writes the design somewhere else must rebase those paths:

* ``relocate_path(uri, from_dir, to_dir)``: one path. Relative and
  ``${KIPRJMOD}`` / ``$(KIPRJMOD)`` paths are rebased (the prefix is kept;
  '\\' is a separator on every platform and comes back as '/'); absolute
  paths and paths through any other variable (``${KICAD9_FOOTPRINT_DIR}/...``)
  are location independent and stay as written. A target on another drive
  (Windows) becomes absolute.
* ``relocate_libraries(design, from_dir, to_dir)``: a NEW design with the
  ``libraries`` block rebased, and the ``source`` files (path, reference,
  erc_baseline, drc_baseline, import_report, via, board), ``variant.from``
  and ``board.outline.dxf`` rebased both on the design and on every
  ``blocks[]`` entry (a composite's block provenance). ``source.files`` names
  files of the source project, not paths: untouched.
* ``resolve_path(uri, base_dir)``: the absolute path a design path names
  (None when it goes through a variable other than KIPRJMOD); ``libs\\X`` and
  ``libs/X`` name the same path on every platform.

Records: ``check_records(design, label, keys)`` raises ValueError naming each
present record (nets, no_connect, net_specs, power_flags, assumptions with
their ids, variant.dropped_refs, libraries, blocks with ref_map / net_map /
drop_refs) that is not of the shape the transforms read, so a malformed
design fails with a message instead of an AttributeError or a corrupt result
deep inside a transform (netlist_schema, which needs jsonschema, checks the
rest).

Footprints:

* ``footprint_locator(design, netlist_dir, stock_dir=None)``: a function
  ``"Nick:Name" -> Path | None`` resolving the nickname in the D1 order
  (libraries block, project fp-lib-table, stock ``<stock_dir>/<nick>.pretty``)
  through libtable.LibraryResolver; ``footprint_file`` is the one-shot form.
* ``footprint_pad_summary(path)``: what a ``.kicad_mod`` (KiCad 5 bare-word
  or KiCad 6+ quoted) holds, by a text scan that skips quoted strings:
  ``{pads, numbered, npth, drilled, electrical, copper}`` (counts; copper is
  True when a pad other than an NPTH sits on a copper layer; electrical =
  numbered pads that are not NPTH, the pads a net can reach).

Stdlib only (+ libtable / sexpr_read, both stdlib-only), Python 3.11-safe.
"""
from __future__ import annotations

import copy
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path, PureWindowsPath
from typing import Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import libtable  # noqa: E402
import sexpr_read as sr  # noqa: E402

PathArg = str | os.PathLike
FootprintLocator = Callable[[str], "Path | None"]

KIPRJMOD_PREFIXES = ("${KIPRJMOD}", "$(KIPRJMOD)")
# source keys holding a file path (netlist_schema _SOURCE_FILES + the source itself)
SOURCE_PATH_KEYS = ("path", "reference", "erc_baseline", "drc_baseline", "import_report",
                    "via", "board")
FOOTPRINT_SUFFIX = ".kicad_mod"
PAD_TYPES = ("smd", "thru_hole", "np_thru_hole", "connect")
LIBRARY_KINDS = ("symbols", "footprints")       # netlist_schema $defs.libraries

_STRING = r'"(?:[^"\\]|\\.)*"'
# a quoted string (skipped) or a pad head: (pad <number> <type>
_PAD_SCAN = re.compile(rf'{_STRING}|\(\s*pad\s+({_STRING}|[^\s()"]+)\s+([A-Za-z_]+)')
_LAYERS = re.compile(rf'\(\s*layers((?:\s+(?:{_STRING}|[^\s()"]+))*)\s*\)')
_LAYER_TOKEN = re.compile(rf'({_STRING})|([^\s"]+)')


# --- paths ------------------------------------------------------------------------------
def _split_kiprjmod(uri: str) -> tuple[str | None, str]:
    """(the KIPRJMOD prefix as written or None, the rest with '/' separators
    and, after a prefix, no leading separator). '\\' is a separator on every
    platform: KiCad writes either, and POSIX path functions would otherwise
    keep 'libs\\X.pretty' as one file name."""
    text = uri.replace("\\", "/")
    for prefix in KIPRJMOD_PREFIXES:
        if text.startswith(prefix):
            return prefix, text[len(prefix):].lstrip("/")
    return None, text


def _is_absolute(text: str) -> bool:
    """Absolute on any platform: a drive / UNC path or a rooted '/...' path."""
    return PureWindowsPath(text).is_absolute() or text.startswith(("/", "\\"))


def _rebasable(uri: object) -> tuple[str | None, str] | None:
    """(prefix, relative rest) of a path that moves with the design; None for
    anything location independent (absolute, another variable) or not a path."""
    if not isinstance(uri, str) or not uri.strip():
        return None
    prefix, rest = _split_kiprjmod(uri)
    if "$" in rest or _is_absolute(rest):
        return None
    return prefix, rest


def relocate_path(uri: str, from_dir: PathArg, to_dir: PathArg) -> str:
    """`uri` (a design path written for a netlist in `from_dir`) rewritten for
    a netlist in `to_dir` (module doc). Non-strings and blanks come back as is."""
    parts = _rebasable(uri)
    if parts is None:
        return uri
    prefix, rest = parts
    target = os.path.normpath(os.path.join(os.path.abspath(from_dir), rest))
    try:
        rel = Path(os.path.relpath(target, os.path.abspath(to_dir))).as_posix()
    except ValueError:                      # another drive: only an absolute path works
        return Path(target).as_posix()
    if prefix is None:
        return rel
    return prefix if rel == "." else f"{prefix}/{rel}"


def resolve_path(uri: str, base_dir: PathArg) -> Path | None:
    """The absolute, normalised path `uri` names for a netlist in `base_dir`;
    None when it goes through a variable other than KIPRJMOD."""
    prefix, rest = _split_kiprjmod(uri)
    if "$" in rest:
        return None
    if _is_absolute(rest):
        return Path(os.path.normpath(rest))
    return Path(os.path.normpath(os.path.join(os.path.abspath(base_dir), rest)))


def _relocated_source(source: object, move: Callable[[str], str]) -> object:
    if not isinstance(source, Mapping):
        return copy.deepcopy(source)
    return {k: (move(v) if k in SOURCE_PATH_KEYS else copy.deepcopy(v))
            for k, v in source.items()}


def _relocated_libraries(libs: object, move: Callable[[str], str]) -> object:
    if not isinstance(libs, Mapping):
        return copy.deepcopy(libs)
    return {kind: ({nick: move(p) for nick, p in table.items()}
                   if isinstance(table, Mapping) else copy.deepcopy(table))
            for kind, table in libs.items()}


def _relocated_board(board: object, move: Callable[[str], str]) -> object:
    spec = board.get("outline") if isinstance(board, Mapping) else None
    if not isinstance(spec, Mapping) or "dxf" not in spec:
        return copy.deepcopy(board)
    return {**copy.deepcopy(dict(board)), "outline": {**spec, "dxf": move(spec["dxf"])}}


def _relocated_variant(variant: object, move: Callable[[str], str]) -> object:
    if not isinstance(variant, Mapping) or "from" not in variant:
        return copy.deepcopy(variant)
    return {**copy.deepcopy(dict(variant)), "from": move(variant["from"])}


def _relocated_provenance(record: Mapping[str, Any], move: Callable[[str], str]) -> dict:
    """A copy of a design or a blocks[] entry with its source files,
    variant.from and board.outline.dxf moved."""
    out = copy.deepcopy(dict(record))
    if "source" in out:
        out["source"] = _relocated_source(record["source"], move)
    if "variant" in out:
        out["variant"] = _relocated_variant(record["variant"], move)
    if "board" in out:
        out["board"] = _relocated_board(record["board"], move)
    return out


def _relocated_blocks(blocks: object, move: Callable[[str], str]) -> object:
    if not isinstance(blocks, list):
        return copy.deepcopy(blocks)
    return [_relocated_provenance(b, move) if isinstance(b, Mapping) else copy.deepcopy(b)
            for b in blocks]


def relocate_libraries(design: Mapping[str, Any], from_dir: PathArg,
                       to_dir: PathArg) -> dict[str, Any]:
    """A NEW design whose relative paths (libraries; source files,
    variant.from and board.outline.dxf of the design and of every blocks[]
    entry) name the same files from a netlist in `to_dir` as they did from
    `from_dir`. Never mutates `design`."""
    def move(uri: str) -> str:
        return relocate_path(uri, from_dir, to_dir)

    out = _relocated_provenance(design, move)
    if "libraries" in out:
        out["libraries"] = _relocated_libraries(design["libraries"], move)
    if "blocks" in out:
        out["blocks"] = _relocated_blocks(design["blocks"], move)
    return out


# --- record shapes ------------------------------------------------------------------------
def _is_name(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _is_pad(value: object) -> bool:
    """A [ref, pad] pair; the pad is a non-empty string or an integer."""
    if not isinstance(value, (list, tuple)) or len(value) != 2 or not _is_name(value[0]):
        return False
    pad = value[1]
    return _is_name(pad) or (isinstance(pad, int) and not isinstance(pad, bool))


def _list_of(value: object, item: Callable[[object], bool]) -> bool:
    return isinstance(value, list) and all(item(v) for v in value)


def _map_of(value: object, item: Callable[[object], bool]) -> bool:
    return isinstance(value, Mapping) and all(_is_name(k) and item(v) for k, v in value.items())


def _is_mapping(value: object) -> bool:
    return isinstance(value, Mapping)


def _is_pad_list(value: object) -> bool:
    return _list_of(value, _is_pad)


def _is_libraries(value: object) -> bool:
    return isinstance(value, Mapping) and all(
        kind in LIBRARY_KINDS and _map_of(table, _is_name) for kind, table in value.items())


def _is_names(value: object) -> bool:
    return _list_of(value, _is_name)


def _is_block(value: object) -> bool:
    return isinstance(value, Mapping) and all(
        value.get(key) is None or isinstance(value[key], Mapping)
        for key in ("ref_map", "net_map")) and (
        value.get("drop_refs") is None or _is_names(value["drop_refs"]))


def _is_nets(value: object) -> bool:
    return _map_of(value, _is_pad_list)


def _is_specs(value: object) -> bool:
    return _map_of(value, _is_mapping)


def _is_assumption(value: object) -> bool:
    return isinstance(value, Mapping) and _is_name(value.get("id"))


def _is_assumptions(value: object) -> bool:
    return _list_of(value, _is_assumption)


def _is_variant(value: object) -> bool:
    return isinstance(value, Mapping) and (
        value.get("dropped_refs") is None or _is_names(value["dropped_refs"]))


def _is_blocks(value: object) -> bool:
    return _list_of(value, _is_block)


RECORD_SHAPES: dict[str, tuple[Callable[[object], bool], str]] = {
    "nets": (_is_nets, "an object {net name: [[ref, pad], ...]}"),
    "no_connect": (_is_pad_list, "a list of [ref, pad] pairs"),
    "net_specs": (_is_specs, "an object {net name: {...}}"),
    "power_flags": (_is_names, "a list of net names"),
    "assumptions": (_is_assumptions, "a list of assumption objects, each with a non-empty "
                                     "string 'id'"),
    "variant": (_is_variant, "an object whose dropped_refs is a list of refs"),
    "libraries": (_is_libraries, "an object {symbols | footprints: {nickname: path}}"),
    "blocks": (_is_blocks, "a list of block objects whose ref_map / net_map are objects "
                           "and drop_refs a list of refs"),
}
RECORD_KEYS = tuple(RECORD_SHAPES)


def check_records(design: Mapping[str, Any], label: str,
                  keys: Iterable[str] = RECORD_KEYS) -> None:
    """Raise ValueError naming every record of `keys` that `design` has (not
    None) in a shape other than RECORD_SHAPES'; `label` names the design."""
    bad = [f"'{key}' must be {RECORD_SHAPES[key][1]}" for key in keys
           if design.get(key) is not None and not RECORD_SHAPES[key][0](design[key])]
    if bad:
        raise ValueError(f"the {label}'s records are malformed: {'; '.join(bad)}")


# --- footprints ---------------------------------------------------------------------------
def split_lib_id(fp_id: object) -> tuple[str, str] | None:
    """("Nick", "Name") of a "Nick:Name" id, else None."""
    if not isinstance(fp_id, str):
        return None
    nick, sep, name = fp_id.partition(":")
    return (nick, name) if sep and nick and name else None


def footprint_locator(design: Mapping[str, Any], netlist_dir: PathArg,
                      stock_dir: PathArg | None = None) -> FootprintLocator:
    """A function "Nick:Name" -> the .kicad_mod Path (None when the nickname
    or the file is not found), resolving nicknames in the D1 order. Raises
    ValueError (libtable.LibTableError) on a malformed libraries block or
    project table."""
    resolver = libtable.LibraryResolver.for_design(netlist_dir, design, None, stock_dir)

    def locate(fp_id: str) -> Path | None:
        parts = split_lib_id(fp_id)
        if parts is None:
            return None
        hit = resolver.resolve(libtable.FOOTPRINT, parts[0])
        if hit is None:
            return None
        path = hit.path / f"{parts[1]}{FOOTPRINT_SUFFIX}"
        return path if path.is_file() else None

    return locate


def footprint_file(design: Mapping[str, Any], netlist_dir: PathArg, fp_id: str,
                   stock_dir: PathArg | None = None) -> Path | None:
    """The .kicad_mod file of `fp_id` ("Nick:Name"): the nickname through the
    design's libraries block (paths relative to `netlist_dir`), the project
    fp-lib-table, else `stock_dir`/<Nick>.pretty; None when not found."""
    return footprint_locator(design, netlist_dir, stock_dir)(fp_id)


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] == '"':
        return re.sub(r"\\(.)", r"\1", token[1:-1])
    return token


def _block_end(text: str, start: int) -> int:
    """The index just past the parenthesised form opening at `start`."""
    depth = 0
    for m in sr.TOKEN_RE.finditer(text, start):
        tok = m.group(0)
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
            if depth == 0:
                return m.end()
    return len(text)


def _on_copper(pad_text: str) -> bool:
    m = _LAYERS.search(pad_text)
    if m is None:
        return False
    names = (_unquote(q) if q else bare for q, bare in _LAYER_TOKEN.findall(m.group(1)))
    return any(name.endswith(".Cu") for name in names)


def _pads(text: str) -> list[tuple[str, str, bool]]:
    """(number, type, on a copper layer) of every pad, quoted strings skipped."""
    pads: list[tuple[str, str, bool]] = []
    pos = 0
    while True:
        m = _PAD_SCAN.search(text, pos)
        if m is None:
            return pads
        if m.group(1) is None:                     # a quoted string: skip it whole
            pos = m.end()
            continue
        end = _block_end(text, m.start())
        pads.append((_unquote(m.group(1)), m.group(2), _on_copper(text[m.start():end])))
        pos = end


def footprint_pad_summary(path: PathArg) -> dict[str, Any]:
    """{pads, numbered, npth, drilled, electrical, copper} of a .kicad_mod
    (module doc). Raises OSError when unreadable and ValueError when the file
    is not a footprint."""
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    if sr.head_of(text) not in ("footprint", "module"):
        raise ValueError(f"{p} is not a footprint (expected '(footprint ...' or '(module ...')")
    pads = _pads(text)
    return {"pads": len(pads),
            "numbered": sum(bool(num) for num, _t, _c in pads),
            "npth": sum(kind == "np_thru_hole" for _n, kind, _c in pads),
            "drilled": sum(kind in ("thru_hole", "np_thru_hole") for _n, kind, _c in pads),
            "electrical": sum(bool(num) and kind != "np_thru_hole" for num, kind, _c in pads),
            "copper": any(cu and kind != "np_thru_hole" for _n, kind, cu in pads)}
