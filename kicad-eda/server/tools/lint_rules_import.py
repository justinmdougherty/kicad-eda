#!/usr/bin/env python3
"""G1 lint rules for imported / rebuilt designs (IM-*), ORDER 40.

A design is an import when it carries ``source``, ``rebuild`` or ``variant``
(ROADMAP D4, rebuild_variant); every other design gets no findings here.

  rule   level    finding
  IM-E1  error    ``rebuild: "keep"`` and a schematic part (not ``board_only``)
                  has neither ``at`` nor x/y (the refs are listed). Parts a
                  composed block added (``blocks[].ref_map`` values,
                  design_blocks) are the pipeline's to place: exempt
  IM-E2  error    ``source.reference`` is set, but the file is missing,
                  unreadable or not a ``kicad-eda.padnets/1`` reference
  IM-E3  error    a ``variant.dropped_refs`` ref still appears in components
                  or nets
  IM-E4  error    a component's ``dnp`` / ``in_bom`` / ``board_only`` is
                  present but not a boolean
  IM-W1  warning  ``source.path`` is missing on disk, or its sha256 differs
                  from ``source.sha256`` (source changed since import)
  IM-W2  warning  ``rebuild: "reflow"`` with ``at`` on more than half of the
                  parts (they stay fixed)
  IM-W3  warning  a footprint file reached through the ``libraries`` block is
                  newer than the D0 dialect: ``(version N)`` > 20241229, or >
                  ``ctx.swig_fp_version_max`` when that was probed (KiCad 9
                  pcbnew cannot load it; re-import with the kicad_dialect
                  downgrade); at most MAX_SAMPLED (50) files are read
  IM-W4  warning  a ``board_only`` part is listed in ``no_connect``

Paths follow D1: relative to ``ctx.netlist_dir``, ``${KIPRJMOD}`` /
``$(KIPRJMOD)``-prefixed (= that directory) or absolute. A relative path
without a netlist directory (``lint_netlist.lint(design)`` with no context)
cannot be located, so its rule says nothing; a path through any other
variable is reported by IM-E2 / IM-W1 and skipped by IM-W3.

Stdlib only (plus the stdlib-only siblings lint_context, design_paths,
fp_dialect and net_equivalence) and Python 3.11-safe. check() never mutates
the design and never raises on a malformed one (shapes are the schema's job).
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PureWindowsPath
from typing import Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import design_paths as dp  # noqa: E402
import fp_dialect  # noqa: E402
from lint_context import LintContext  # noqa: E402
from net_equivalence import REFERENCE_FORMAT, read_reference  # noqa: E402

ORDER = 40

IMPORT_KEYS = ("source", "rebuild", "variant")     # any of them set: an imported design
FLAG_KEYS = ("dnp", "in_bom", "board_only")        # D4 component flags: booleans
MAX_LISTED = 20                 # refs / files named in one message
MAX_SAMPLED = 50                # footprint files IM-W3 reads at most
HASH_CHUNK = 1 << 20            # sha256 read size (bytes)
SHORT_VALUE = 60                # a bad value shown in a message, at most
D0_FP_VERSION = fp_dialect.KICAD9_FOOTPRINT_VERSION
DOWNGRADE_HINT = ("re-import (kicad_dialect downgrade: python server/tools/kicad_dialect.py "
                  "fp <src.pretty> <dst.pretty>)")

Findings = tuple[list[str], list[str]]


def is_imported(design: Mapping[str, Any]) -> bool:
    """True when the design came from an import or a rebuild (module doc)."""
    return any(design.get(key) is not None for key in IMPORT_KEYS)


def check(design: Mapping[str, Any], ctx: LintContext) -> Findings:
    """IM-E1..IM-E4 errors and IM-W1..IM-W4 warnings, in rule order."""
    if not isinstance(design, Mapping) or not is_imported(design):
        return [], []
    comps = _components(design)
    raw_source = design.get("source")
    source: Mapping[str, Any] = raw_source if isinstance(raw_source, Mapping) else {}
    netlist_dir = ctx.netlist_dir
    errors = [*_e1_unplaced(design, comps), *_e2_reference(source, netlist_dir),
              *_e3_dropped(design, comps), *_e4_flags(comps)]
    warnings = [*_w1_source(source, netlist_dir), *_w2_reflow_fixed(design, comps),
                *_w3_new_footprints(design, comps, ctx), *_w4_board_only_nc(design, comps)]
    return errors, warnings


# --- helpers ----------------------------------------------------------------------------------
def _components(design: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    comps = design.get("components")
    return [c for c in comps if isinstance(c, Mapping)] if isinstance(comps, list) else []


def _ref(comp: Mapping[str, Any]) -> str:
    ref = comp.get("ref")
    return "?" if ref is None else str(ref)


def _listed(items: Sequence[str]) -> str:
    more = len(items) - MAX_LISTED
    return ", ".join(items[:MAX_LISTED]) + (f" (+{more} more)" if more > 0 else "")


def _shown(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, default=repr)
    return text if len(text) <= SHORT_VALUE else text[:SHORT_VALUE - 3] + "..."


def _placed(comp: Mapping[str, Any]) -> bool:
    """Placed by the source: a D3 ``at`` anchor or the x/y centre pair."""
    return comp.get("at") is not None or (comp.get("x") is not None
                                          and comp.get("y") is not None)


def _without_kiprjmod(uri: str) -> str:
    for prefix in dp.KIPRJMOD_PREFIXES:
        if uri.startswith(prefix):
            return uri[len(prefix):].lstrip("/\\")
    return uri


def _is_absolute(text: str) -> bool:
    return PureWindowsPath(text).is_absolute() or text.startswith(("/", "\\"))


def _locate(uri: object, netlist_dir: Path | None) -> tuple[Path | None, str]:
    """(the file a design path names, '') or (None, why it cannot be located).
    ``why`` is '' for a relative path without a netlist directory: not
    judgeable here, so the caller says nothing."""
    if not isinstance(uri, str) or not uri.strip():
        return None, f"is not a path (got {_shown(uri)})"
    rest = _without_kiprjmod(uri)
    if "$" in rest:
        return None, f"{uri} goes through a variable other than ${{KIPRJMOD}}: unresolvable"
    if netlist_dir is None and not _is_absolute(rest):
        return None, ""
    return dp.resolve_path(uri, netlist_dir if netlist_dir is not None else "."), ""


def _net_refs(design: Mapping[str, Any]) -> dict[str, list[str]]:
    """ref -> the nets (in net order) that hold one of its pads."""
    nets = design.get("nets")
    out: dict[str, list[str]] = {}
    for name, pads in (nets.items() if isinstance(nets, Mapping) else ()):
        refs = {str(p[0]) for p in (pads if isinstance(pads, list) else ())
                if isinstance(p, (list, tuple)) and p}
        for ref in refs:
            out.setdefault(ref, []).append(str(name))
    return out


# --- errors -----------------------------------------------------------------------------------
def _block_refs(design: Mapping[str, Any]) -> set[str]:
    """The refs composed blocks added (every ``blocks[].ref_map`` value)."""
    blocks = design.get("blocks")
    maps = [b.get("ref_map") for b in (blocks if isinstance(blocks, list) else ())
            if isinstance(b, Mapping)]
    return {str(ref) for m in maps if isinstance(m, Mapping) for ref in m.values()}


def _e1_unplaced(design: Mapping[str, Any], comps: list[Mapping[str, Any]]) -> list[str]:
    if design.get("rebuild") != "keep":
        return []
    added = _block_refs(design)
    refs = [_ref(c) for c in comps if c.get("board_only") is not True and not _placed(c)
            and _ref(c) not in added]
    if not refs:
        return []
    return [f"IM-E1 rebuild 'keep' but {len(refs)} part(s) have neither 'at' nor x/y: "
            f"{_listed(refs)} (a keep rebuild places every schematic part where the source "
            "has it: give them 'at', or rebuild with 'reflow')"]


def _reference_problem(path: Path) -> str:
    """'' when ``path`` is a readable kicad-eda.padnets/1 reference, else why not."""
    if not path.exists():
        return f"the file is missing ({path})"
    if not path.is_file():
        return f"{path} is not a file"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:          # UnicodeDecodeError is a ValueError
        return f"unreadable ({path}): {e}"
    fmt = data.get("format") if isinstance(data, Mapping) else None
    if fmt != REFERENCE_FORMAT:
        return (f"{path} is not a {REFERENCE_FORMAT} reference (format {_shown(fmt)}); "
                "re-freeze it with net_equivalence.write_reference")
    try:
        read_reference(path)
    except (OSError, ValueError, TypeError) as e:
        return f"unreadable as a {REFERENCE_FORMAT} reference: {e}"
    return ""


def _e2_reference(source: Mapping[str, Any], netlist_dir: Path | None) -> list[str]:
    uri = source.get("reference")
    if uri is None or uri == "":
        return []
    path, why = _locate(uri, netlist_dir)
    if path is None:
        return [f"IM-E2 source.reference {why}"] if why else []
    problem = _reference_problem(path)
    return [f"IM-E2 source.reference {uri}: {problem}"] if problem else []


def _e3_dropped(design: Mapping[str, Any], comps: list[Mapping[str, Any]]) -> list[str]:
    variant = design.get("variant")
    dropped = variant.get("dropped_refs") if isinstance(variant, Mapping) else None
    if not isinstance(dropped, list):
        return []
    comp_refs = {_ref(c) for c in comps}
    net_refs = _net_refs(design)
    errors: list[str] = []
    for ref in dict.fromkeys(r for r in dropped if isinstance(r, str)):
        where = (["components"] if ref in comp_refs else []) + \
            ([f"nets {_listed([repr(n) for n in net_refs[ref]])}"] if ref in net_refs else [])
        if where:
            errors.append(f"IM-E3 variant.dropped_refs names {ref}, but it is still in "
                          + " and ".join(where))
    return errors


def _e4_flags(comps: Iterable[Mapping[str, Any]]) -> list[str]:
    return [f"IM-E4 {_ref(c)}: '{key}' must be true or false, got {_shown(c[key])}"
            for c in comps for key in FLAG_KEYS if key in c and not isinstance(c[key], bool)]


# --- warnings ---------------------------------------------------------------------------------
def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_problem(path: Path, sha: object) -> str:
    """'' when the source file is there and (if a sha256 is recorded) unchanged."""
    if not path.exists():
        return f"missing on disk ({path}): source changed since import (moved or deleted?)"
    if not path.is_file():
        return f"{path} is not a file"
    if not isinstance(sha, str) or not sha:
        return ""
    try:
        digest = _sha256_of(path)
    except OSError as e:
        return f"cannot read {path} to check its sha256: {e}"
    if digest.lower() == sha.lower():
        return ""
    return (f"its sha256 {digest[:12]}... differs from source.sha256 {sha[:12]}...: "
            "source changed since import (re-import it, or re-check the rebuild)")


def _w1_source(source: Mapping[str, Any], netlist_dir: Path | None) -> list[str]:
    uri = source.get("path")
    if uri is None or uri == "":
        return []
    path, why = _locate(uri, netlist_dir)
    if path is None:
        return [f"IM-W1 source.path {why}"] if why else []
    problem = _source_problem(path, source.get("sha256"))
    return [f"IM-W1 source.path {uri}: {problem}"] if problem else []


def _w2_reflow_fixed(design: Mapping[str, Any], comps: list[Mapping[str, Any]]) -> list[str]:
    if design.get("rebuild") != "reflow" or not comps:
        return []
    fixed = sum(c.get("at") is not None for c in comps)
    if 2 * fixed <= len(comps):
        return []
    return [f"IM-W2 rebuild 'reflow' but {fixed} of {len(comps)} part(s) carry 'at' and stay "
            "fixed where the source had them; drop 'at' from the parts the reflow should "
            "move, or rebuild with 'keep'"]


def _block_footprints(design: Mapping[str, Any], comps: list[Mapping[str, Any]],
                      netlist_dir: Path | None) -> list[tuple[str, Path]]:
    """(nickname, .kicad_mod) of the existing footprint files the components
    reach through the libraries block, distinct, in component order, at most
    MAX_SAMPLED of them."""
    libs = design.get("libraries")
    table = libs.get("footprints") if isinstance(libs, Mapping) else None
    if not isinstance(table, Mapping):
        return []
    lib_dirs: dict[str, Path | None] = {}
    checked: set[Path] = set()
    found: list[tuple[str, Path]] = []
    for comp in comps:
        parts = dp.split_lib_id(comp.get("footprint"))
        if parts is None or parts[0] not in table:
            continue
        nick, name = parts
        if nick not in lib_dirs:
            lib_dirs[nick] = _locate(table[nick], netlist_dir)[0]
        lib_dir = lib_dirs[nick]
        path = lib_dir / f"{name}{dp.FOOTPRINT_SUFFIX}" if lib_dir is not None else None
        if path is None or path in checked:
            continue
        checked.add(path)
        if path.is_file():
            found.append((nick, path))
            if len(found) == MAX_SAMPLED:
                break
    return found


def _too_new_message(nick: str, uri: object, files: list[str], sampled: int,
                     limit: int) -> str:
    d0 = limit == D0_FP_VERSION
    what = "the D0 dialect" if d0 else "what the SWIG pcbnew loads"
    who = "KiCad 9 pcbnew" if d0 else f"the SWIG pcbnew (footprints up to (version {limit}))"
    return (f"IM-W3 footprint library {nick} ({uri}): {len(files)} of {sampled} sampled "
            f"file(s) newer than {what} ((version N) > {limit}): {_listed(files)} - {who} "
            f"cannot load it; {DOWNGRADE_HINT}")


def _w3_new_footprints(design: Mapping[str, Any], comps: list[Mapping[str, Any]],
                       ctx: LintContext) -> list[str]:
    files = _block_footprints(design, comps, ctx.netlist_dir)
    if not files:
        return []
    probed = ctx.swig_fp_version_max
    limit = probed if probed is not None else D0_FP_VERSION
    sampled = Counter(nick for nick, _path in files)
    too_new: dict[str, list[str]] = {}
    for nick, path in files:
        version = fp_dialect.footprint_format_version(path)
        if version is not None and version > limit:
            too_new.setdefault(nick, []).append(f"{path.name} (version {version})")
    table = design["libraries"]["footprints"]       # _block_footprints found files: it exists
    return [_too_new_message(nick, table[nick], names, sampled[nick], limit)
            for nick, names in too_new.items()]


def _w4_board_only_nc(design: Mapping[str, Any], comps: list[Mapping[str, Any]]) -> list[str]:
    board_only = {_ref(c) for c in comps if c.get("board_only") is True}
    no_connect = design.get("no_connect")
    if not board_only or not isinstance(no_connect, list):
        return []
    pads: dict[str, list[str]] = {}
    for entry in no_connect:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2 and str(entry[0]) in board_only:
            pads.setdefault(str(entry[0]), []).append(f"{entry[0]}.{entry[1]}")
    return [f"IM-W4 board_only part {ref} is listed in no_connect ({_listed(names)}): it is "
            "not in the schematic, so there is no pin to flag; drop those entries"
            for ref, names in pads.items()]
