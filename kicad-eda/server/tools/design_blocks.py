#!/usr/bin/env python3
"""Compose a design from a base and a block (ROADMAP_DECISIONS D4: a rebuilt
core plus new modules, e.g. a Feather M0 RFM95 core + an LG290P GNSS block).

``compose(base, block, *, base_dir, block_dir, prefix, ...)`` returns a
``ComposeResult(design, ref_map, net_map, problems)``; the composite keeps the
base's ``source`` / ``board`` / ``rebuild`` and its paths relative to
`base_dir`. Rules:

* References: a block ref keeps its name unless a base ref has it (dropped
  base refs included, so a replaced part's ref is never reused); a colliding
  ref is renumbered to the natural next free number of its prefix (one more
  than the highest in use: base R1..R9 -> block R1 becomes R10). An explicit
  `ref_map` {block ref: new ref} wins; the result's ref_map lists EVERY block
  ref, identity entries included.
* Nets: a block net goes to ``net_map[name]`` when given (an existing base
  net joins it), else joins the base net of the same name only when listed in
  `join_nets` (default GND; ``+3V3`` joins only when listed), else becomes
  ``<prefix>_<name>`` (``private_nets="keep"`` keeps the name unless the base
  has it). A taken name gets ``_2``, ``_3``...; each such case is a problem.
* `drop_refs` are BASE refs this composition removes (e.g. the micro-USB
  connector a USB-C block replaces): their pads leave the nets; a base net
  left with one pad turns it into a ``no_connect`` (with an assumption), an
  emptied net disappears with its net_specs / power_flags entries.
* Merged with renames: ``no_connect``, ``net_specs`` (``diff_pair`` names
  follow; on a joined net the base's spec wins), ``power_flags``,
  ``assumptions`` (block ids prefixed ``<prefix>-``; an id already in the
  composite gets ``_2``, ``_3``..., each a problem) and ``libraries`` (block
  paths relocated from `block_dir`; the same nickname naming another library
  is a problem and the base's stays).
* Block parts lose ``at`` / ``x`` / ``y`` unless `keep_placement` (``rot``
  stays: a preference, not a position).
* Provenance: ``blocks`` gains ``{name: prefix, design, source, rebuild,
  ref_map, net_map, drop_refs, board, variant}`` (a nested block's own entries
  follow as ``<prefix>/<name>`` with their maps carried through). The prefix
  must not already name a base block (``blocks[].name`` or its part before
  '/'): a ValueError suggests a free one. Other block keys that differ from
  the base's (copper_layers, planes, ...) are ignored, each as a problem.

Malformed records (design_paths.check_records) raise ValueError naming them.
Pure (new dicts, inputs untouched); stdlib + design_paths.

CLI: ``design_blocks.py <base.json> <block.json> <out.json> --prefix GNSS
[--net-map JSON|file] [--ref-map JSON|file] [--join-nets GND,+3V3]
[--drop-refs J1] [--keep-nets] [--keep-placement]`` writes the composite with
its paths relocated to out.json's directory and prints a JSON summary; exit 0
written, 1 written with problems, 2 usage/IO error (nothing written).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import design_paths as dp  # noqa: E402

PRIVATE_NET_MODES = ("prefix", "keep")
DEFAULT_JOIN_NETS = ("GND",)
PLACEMENT_KEYS = ("at", "x", "y")
LIBRARY_KINDS = dp.LIBRARY_KINDS
# block keys compose merges or records; any other differing key is a problem
HANDLED_KEYS = frozenset({"name", "title", "components", "nets", "no_connect", "net_specs",
                          "power_flags", "assumptions", "libraries", "board", "source",
                          "rebuild", "variant", "blocks"})
_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")
_REF_RE = re.compile(r"^(.*?)(\d+)$")


@dataclass(frozen=True)
class ComposeResult:
    """compose's result: the composite design (paths relative to base_dir),
    every block ref old -> new, every block net -> its composite net."""
    design: dict[str, Any]
    ref_map: dict[str, str]
    net_map: dict[str, str]
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "design": copy.deepcopy(self.design),
                "ref_map": dict(self.ref_map), "net_map": dict(self.net_map),
                "problems": list(self.problems)}


@dataclass(frozen=True)
class _Maps:
    refs: dict[str, str]
    nets: dict[str, str]
    dropped: frozenset[str]
    prefix: str


# --- arguments ------------------------------------------------------------------------------
def _names(label: str, value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise TypeError(f"{label} must be a list of names, not {value!r}")
    names = tuple(value)
    if not all(isinstance(n, str) and n for n in names):
        raise TypeError(f"{label} must be a list of non-empty strings, not {names!r}")
    return names


def _check_options(prefix: object, private_nets: str, join_nets: object,
                   drop_refs: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not isinstance(prefix, str) or not _PREFIX_RE.match(prefix):
        raise ValueError(f"prefix must be a short name like 'GNSS' (letters, digits, _ . + -), "
                         f"got {prefix!r}")
    if private_nets not in PRIVATE_NET_MODES:
        raise ValueError(f"private_nets must be one of {PRIVATE_NET_MODES}, got {private_nets!r}")
    return _names("join_nets", join_nets), _names("drop_refs", drop_refs)


def _parts(design: object, label: str) -> tuple[list[Mapping[str, Any]], Mapping[str, list]]:
    comps = design.get("components") if isinstance(design, Mapping) else None
    if not isinstance(comps, list) or not all(isinstance(c, Mapping)
                                              and isinstance(c.get("ref"), str) for c in comps):
        raise ValueError(f"the {label} needs 'components': a list of objects with a 'ref'")
    dp.check_records(design, label)
    return comps, design.get("nets") or {}


def _check_block_pads(block: Mapping[str, Any], block_comps: list[Mapping[str, Any]],
                      block_nets: Mapping[str, list]) -> None:
    """Block nets / no_connect may name only block parts: compose renames
    those, so a pad of another ref would silently wire the base's part."""
    refs = {c["ref"] for c in block_comps}
    in_nets = [f"{name}: {r}.{p}" for name, pads in block_nets.items() for r, p in pads
               if r not in refs]
    in_nc = [f"{r}.{p}" for r, p in block.get("no_connect") or [] if r not in refs]
    bad = ([f"nets ({'; '.join(in_nets)})"] if in_nets else []) + \
        ([f"no_connect ({', '.join(in_nc)})"] if in_nc else [])
    if bad:
        raise ValueError(f"the block's {' and '.join(bad)} name refs that are not block parts "
                         "(lint E4); compose renames only block parts, so such a pad would "
                         "wire a base part")


def _retired(base: Mapping[str, Any]) -> frozenset[str]:
    """Refs the base dropped earlier (variant.dropped_refs, every
    blocks[].drop_refs): never given to a block part. Shapes are checked."""
    variant = base.get("variant") or {}
    return frozenset([*(variant.get("dropped_refs") or []),
                      *(r for b in base.get("blocks") or [] for r in b.get("drop_refs") or [])])


def _check_prefix_free(base: Mapping[str, Any], prefix: str) -> None:
    """The prefix names this composition in ``blocks``: it must be new."""
    names = {str(b.get("name")).split("/", 1)[0] for b in base.get("blocks") or []}
    if prefix in names:
        raise ValueError(f"prefix {prefix!r} already names a block of the base (blocks[].name: "
                         f"{', '.join(sorted(names))}); each composition needs its own prefix, "
                         f"e.g. {_free_name(prefix, names)!r}")


# --- references -----------------------------------------------------------------------------
def _split_ref(ref: str) -> tuple[str, int | None]:
    m = _REF_RE.match(ref)
    return (m.group(1), int(m.group(2))) if m else (ref, None)


def _next_free(ref: str, used: set[str]) -> str:
    """One more than the highest number of `ref`'s prefix in use ("MK" -> "MK1").
    Free by construction: a prefix never ends in a digit, so f"{prefix}{n}"
    splits back to (prefix, n) and n exceeds every number in use."""
    prefix = _split_ref(ref)[0]
    top = max((n for p, n in map(_split_ref, used) if p == prefix and n is not None), default=0)
    return f"{prefix}{top + 1}"


def _explicit_refs(ref_map: object, block_refs: list[str], surviving: frozenset[str],
                   retired: frozenset[str]) -> dict[str, str]:
    if ref_map is None or ref_map == "auto":
        return {}
    if not isinstance(ref_map, Mapping):
        raise TypeError(f"ref_map must be 'auto' or a mapping {{block ref: new ref}}, "
                        f"not {ref_map!r}")
    out: dict[str, str] = {}
    for old, new in ref_map.items():
        if old not in block_refs:
            raise ValueError(f"ref_map: {old!r} is not a block ref")
        if not isinstance(new, str) or not new.strip():
            raise ValueError(f"ref_map: empty new reference for {old}")
        if new in out.values():
            raise ValueError(f"ref_map sends {next(k for k, v in out.items() if v == new)} and "
                             f"{old} both to {new}")
        if new in surviving:
            raise ValueError(f"ref_map: {old} -> {new} collides with the base ref {new}")
        if new in retired:
            raise ValueError(f"ref_map: {old} -> {new} reuses {new}, a ref the base dropped "
                             "(variant.dropped_refs, blocks[].drop_refs or this drop_refs); a "
                             "dropped part's ref is never reused")
        out[old] = new
    for old, new in out.items():
        if new in block_refs and new not in out:
            raise ValueError(f"ref_map: {old} -> {new} is the block ref {new}, which keeps its "
                             f"name (map {new} too)")
    return out


def _ref_map(block_refs: list[str], reserved: Iterable[str],
             explicit: Mapping[str, str]) -> dict[str, str]:
    """Every block ref -> its composite ref; `reserved` = base refs (dropped
    ones included) and the base's retired refs."""
    taken = set(reserved) | set(explicit.values())
    keepers = {r for r in block_refs if r not in explicit and r not in taken}
    used = taken | keepers
    out: dict[str, str] = {}
    for ref in block_refs:
        if ref in explicit:
            out[ref] = explicit[ref]
        elif ref in keepers:
            out[ref] = ref
        else:
            out[ref] = _next_free(ref, used)
            used.add(out[ref])
    return out


# --- nets -----------------------------------------------------------------------------------
def _explicit_nets(net_map: object, block_nets: Mapping[str, list]) -> dict[str, str]:
    if net_map is None:
        return {}
    if not isinstance(net_map, Mapping):
        raise TypeError(f"net_map must be a mapping {{block net: net here}}, not {net_map!r}")
    unknown = [str(k) for k in net_map if k not in block_nets]
    if unknown:
        raise ValueError(f"net_map: {', '.join(unknown)} not a block net")
    empty = [str(k) for k, v in net_map.items() if not isinstance(v, str) or not v]
    if empty:
        raise ValueError(f"net_map: empty target net for {', '.join(empty)}")
    return dict(net_map)


def _free_name(want: str, taken: set[str]) -> str:
    n = 2
    name = want
    while name in taken:
        name = f"{want}_{n}"
        n += 1
    return name


def _net_map(block_nets: Mapping[str, list], base_nets: Mapping[str, list],
             explicit: Mapping[str, str], joins: tuple[str, ...], prefix: str,
             private_nets: str) -> tuple[dict[str, str], list[str]]:
    taken = set(base_nets) | set(explicit.values())
    out: dict[str, str] = {}
    problems: list[str] = []
    for name in block_nets:
        if name in explicit:
            out[name] = explicit[name]
            continue
        if name in joins:
            out[name] = name
            if name not in base_nets:
                problems.append(f"join net {name!r} is not a base net: the block's {name} "
                                "stays a separate net")
            continue
        want = name if private_nets == "keep" else f"{prefix}_{name}"
        if private_nets == "keep" and name in base_nets:
            want = f"{prefix}_{name}"
            problems.append(f"block net {name!r} collides with the base net of that name (not "
                            f"in join_nets); prefixed")
        new = _free_name(want, taken)
        if new != want:
            problems.append(f"block net {name!r}: {want!r} is taken in the composite; named "
                            f"{new!r}")
        taken.add(new)
        out[name] = new
    return out, problems


def _renamed(pads: Iterable[list], refs: Mapping[str, str]) -> list[list]:
    return [[refs.get(ref, ref), pad] for ref, pad in pads]


def _merged_nets(base_nets: Mapping[str, list], block_nets: Mapping[str, list],
                 maps: _Maps) -> tuple[dict[str, list], list[str]]:
    """(composite nets, base nets that lost pads to drop_refs)."""
    out = {name: [list(p) for p in pads if p[0] not in maps.dropped]
           for name, pads in base_nets.items()}
    touched = [name for name, pads in base_nets.items() if any(p[0] in maps.dropped for p in pads)]
    for name, pads in block_nets.items():
        have = out.get(maps.nets[name], [])
        out[maps.nets[name]] = have + [p for p in _renamed(pads, maps.refs) if p not in have]
    return out, touched


def _settled(nets: dict[str, list], touched: list[str]) -> tuple[dict, list[str], list[tuple]]:
    """Drop-touched nets with fewer than two pads: (nets, removed, lone (pad, net))."""
    removed = [n for n in touched if len(nets[n]) < 2]
    lone = [(nets[n][0], n) for n in removed if nets[n]]
    return {n: pads for n, pads in nets.items() if n not in removed}, removed, lone


def _unique(items: Iterable[Any]) -> list[Any]:
    out: list[Any] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


# --- merged records -------------------------------------------------------------------------
def _merged_specs(base: Mapping[str, Any], block: Mapping[str, Any], maps: _Maps,
                  removed: list[str]) -> tuple[dict[str, Any], list[str]]:
    base_specs = base.get("net_specs") or {}
    out = {n: copy.deepcopy(s) for n, s in base_specs.items() if n not in removed}
    problems = [f"net_specs entry for {n} removed: the net vanished with the dropped base parts"
                for n in base_specs if n in removed]
    for name, spec in (block.get("net_specs") or {}).items():
        if name not in maps.nets:
            problems.append(f"block net_specs entry {name!r} names no block net; dropped")
            continue
        spec = copy.deepcopy(dict(spec))
        pair = spec.get("diff_pair")
        if isinstance(pair, str) and pair in maps.nets:
            spec["diff_pair"] = maps.nets[pair]
        target = maps.nets[name]
        if target not in out:
            out[target] = spec
        elif out[target] != spec:
            problems.append(f"net_specs {target}: the base's {out[target]} and the block's "
                            f"{spec} differ; the base's is kept")
    return out, problems


def _merged_flags(base: Mapping[str, Any], block: Mapping[str, Any], maps: _Maps,
                  removed: list[str]) -> tuple[list[str], list[str]]:
    flags = base.get("power_flags") or []
    problems = [f"power_flags entry {f} removed: the net vanished with the dropped base parts"
                for f in flags if f in removed]
    mapped = [maps.nets.get(f, f) for f in block.get("power_flags") or []]
    return _unique([*(f for f in flags if f not in removed), *mapped]), problems


def _lone_assumptions(lone: list[tuple], maps: _Maps, taken: set[str]) -> list[dict[str, Any]]:
    """One no_connect assumption per lone pad, ids compose-<prefix>-<n> not in `taken`."""
    out: list[dict[str, Any]] = []
    n = 0
    for pad, net in lone:
        n += 1
        while f"compose-{maps.prefix}-{n}" in taken:
            n += 1
        out.append({"id": f"compose-{maps.prefix}-{n}", "topic": "no_connect",
                    "value": f"{pad[0]}.{pad[1]}",
                    "rationale": f"net {net} lost its other pads with the dropped base parts "
                                 f"{', '.join(sorted(maps.dropped))}; the pad is left "
                                 "unconnected (D4)", "confidence": "med"})
    return out


def _merged_assumptions(base: Mapping[str, Any], block: Mapping[str, Any], maps: _Maps,
                        lone: list[tuple]) -> tuple[list[dict[str, Any]], list[str]]:
    """(base + block assumptions with unique ids + lone-pad assumptions, problems)."""
    out = [copy.deepcopy(dict(a)) for a in base.get("assumptions") or []]
    taken = {str(a.get("id")) for a in out}
    problems: list[str] = []
    for a in block.get("assumptions") or []:
        want = f"{maps.prefix}-{a.get('id')}"
        new = _free_name(want, taken)
        if new != want:
            problems.append(f"block assumption id {want!r} is taken in the composite; "
                            f"renamed {new!r}")
        taken.add(new)
        out.append({**copy.deepcopy(dict(a)), "id": new})
    return out + _lone_assumptions(lone, maps, taken), problems


def _same_library(a: str, b: str, base_dir: dp.PathArg) -> bool:
    pa, pb = dp.resolve_path(a, base_dir), dp.resolve_path(b, base_dir)
    if pa is None or pb is None:
        return a == b
    return os.path.normcase(str(pa)) == os.path.normcase(str(pb))


def _merged_libraries(base: Mapping[str, Any], block: Mapping[str, Any],
                      base_dir: dp.PathArg) -> tuple[dict[str, Any], list[str]]:
    out = copy.deepcopy(dict(base.get("libraries") or {}))
    problems: list[str] = []
    theirs = block.get("libraries") or {}
    for kind in LIBRARY_KINDS:
        table = dict(out.get(kind) or {})
        for nick, path in (theirs.get(kind) or {}).items():
            have = table.get(nick)
            if have is None:
                table[nick] = path
            elif not _same_library(have, path, base_dir):
                problems.append(f"libraries.{kind} '{nick}': the base uses {have!r} and the block "
                                f"{path!r}, a different library; the base's is kept, so the "
                                f"block's '{nick}:' ids resolve to it")
        if table:
            out[kind] = table
    return out, problems


# --- provenance -----------------------------------------------------------------------------
def _block_entries(base: Mapping[str, Any], block: Mapping[str, Any], maps: _Maps,
                   drops: tuple[str, ...]) -> list[dict[str, Any]]:
    entry: dict[str, Any] = {"name": maps.prefix}
    if isinstance(block.get("name"), str) and block["name"]:
        entry["design"] = block["name"]
    entry.update(source=copy.deepcopy(block.get("source")), rebuild=block.get("rebuild"),
                 ref_map=dict(maps.refs), net_map=dict(maps.nets), drop_refs=list(drops))
    for key in ("board", "variant"):
        if block.get(key) is not None:
            entry[key] = copy.deepcopy(block[key])
    nested = [{**copy.deepcopy(dict(b)), "name": f"{maps.prefix}/{b.get('name')}",
               "ref_map": {k: maps.refs.get(v, v) for k, v in (b.get("ref_map") or {}).items()},
               "net_map": {k: maps.nets.get(v, v) for k, v in (b.get("net_map") or {}).items()}}
              for b in block.get("blocks") or [] if isinstance(b, Mapping)]
    return [*copy.deepcopy(list(base.get("blocks") or [])), entry, *nested]


def _ignored_keys(base: Mapping[str, Any], block: Mapping[str, Any]) -> list[str]:
    return [f"block key '{key}' ignored: the base's value applies" for key in block
            if key not in HANDLED_KEYS and block[key] != base.get(key)]


def _components(base_comps: list[Mapping[str, Any]], block_comps: list[Mapping[str, Any]],
                maps: _Maps, keep_placement: bool) -> list[dict[str, Any]]:
    out = [copy.deepcopy(dict(c)) for c in base_comps if c["ref"] not in maps.dropped]
    out += [{"ref": maps.refs[c["ref"]],
             **{k: copy.deepcopy(v) for k, v in c.items()
                if k != "ref" and (keep_placement or k not in PLACEMENT_KEYS)}}
            for c in block_comps]
    return out


# --- compose --------------------------------------------------------------------------------
def _maps(base_comps: list, block_comps: list, base_nets: Mapping, block_nets: Mapping,
          opts: Mapping[str, Any]) -> tuple[_Maps, list[str]]:
    base_refs = [c["ref"] for c in base_comps]
    unknown = [r for r in opts["drops"] if r not in base_refs]
    if unknown:
        raise ValueError(f"drop_refs: {', '.join(unknown)} not base ref(s) (drop_refs remove "
                         "BASE parts)")
    dropped = frozenset(opts["drops"])
    retired = opts["retired"] | dropped
    block_refs = [c["ref"] for c in block_comps]
    explicit = _explicit_refs(opts["ref_map"], block_refs,
                              frozenset(r for r in base_refs if r not in dropped), retired)
    nets, problems = _net_map(block_nets, base_nets, _explicit_nets(opts["net_map"], block_nets),
                              opts["joins"], opts["prefix"], opts["private_nets"])
    if len(set(block_refs)) != len(block_refs):
        problems.append("the block has duplicate refs; each maps once (lint E5)")
    refs = _ref_map(block_refs, [*base_refs, *retired], explicit)
    return _Maps(refs, nets, dropped, opts["prefix"]), problems


def _lone_problems(removed: list[str], lone: list[tuple]) -> list[str]:
    lone_nets = {net for _pad, net in lone}
    return ([f"net {n} lost all its pads with the dropped base parts; removed"
             for n in removed if n not in lone_nets]
            + [f"net {net} has one pad left after drop_refs: {pad[0]}.{pad[1]} is now no_connect"
               for pad, net in lone])


def compose(base: Mapping[str, Any], block: Mapping[str, Any], *, base_dir: dp.PathArg,
            block_dir: dp.PathArg, prefix: str, ref_map: str | Mapping[str, str] = "auto",
            net_map: Mapping[str, str] | None = None,
            join_nets: Iterable[str] = DEFAULT_JOIN_NETS, private_nets: str = "prefix",
            drop_refs: Iterable[str] = (), keep_placement: bool = False) -> ComposeResult:
    """The composite of `base` and `block` (module doc). Raises ValueError /
    TypeError on bad arguments; everything else is reported in ``problems``."""
    joins, drops = _check_options(prefix, private_nets, join_nets, drop_refs)
    base_comps, base_nets = _parts(base, "base")
    block_comps, block_nets = _parts(block, "block")
    _check_block_pads(block, block_comps, block_nets)
    _check_prefix_free(base, prefix)
    block = dp.relocate_libraries(block, block_dir, base_dir)
    maps, problems = _maps(base_comps, block_comps, base_nets, block_nets,
                           {"drops": drops, "retired": _retired(base), "ref_map": ref_map,
                            "net_map": net_map, "joins": joins, "prefix": prefix,
                            "private_nets": private_nets})
    nets, removed, lone = _settled(*_merged_nets(base_nets, block_nets, maps))
    no_connect = _unique([*([list(p) for p in base.get("no_connect") or []
                             if p[0] not in maps.dropped]),
                          *_renamed(block.get("no_connect") or [], maps.refs),
                          *(pad for pad, _net in lone)])
    specs, spec_problems = _merged_specs(base, block, maps, removed)
    flags, flag_problems = _merged_flags(base, block, maps, removed)
    libraries, lib_problems = _merged_libraries(base, block, base_dir)
    assumptions, id_problems = _merged_assumptions(base, block, maps, lone)
    updates = {"components": _components(base_comps, block_comps, maps, keep_placement),
               "nets": nets, "no_connect": no_connect, "net_specs": specs, "power_flags": flags,
               "assumptions": assumptions, "libraries": libraries,
               "blocks": _block_entries(base, block, maps, drops)}
    design = {**copy.deepcopy(dict(base)),
              **{k: v for k, v in updates.items() if v or k in base}}
    problems += (_lone_problems(removed, lone) + spec_problems + flag_problems + lib_problems
                 + id_problems + _ignored_keys(base, block))
    return ComposeResult(design, dict(maps.refs), dict(maps.nets), tuple(problems))


# --- CLI ------------------------------------------------------------------------------------
def _json_arg(raw: str | None) -> Any:
    """Inline JSON (starting with '{') or a JSON file (a UTF-8 BOM is fine);
    None stays None."""
    if raw is None:
        return None
    text = raw.strip()
    if text.startswith("{"):
        return json.loads(text)
    return json.loads(Path(text).read_text(encoding="utf-8-sig"))


def _load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a netlist (a JSON object)")
    return data


def _csv(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]


def _same_file(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _run(args: argparse.Namespace) -> tuple[ComposeResult, Path]:
    base_path, block_path, out = Path(args.base), Path(args.block), Path(args.out)
    if _same_file(out, base_path) or _same_file(out, block_path):
        raise ValueError(f"refusing to overwrite an input with the composite: {out}")
    res = compose(_load(base_path), _load(block_path), base_dir=base_path.parent,
                  block_dir=block_path.parent, prefix=args.prefix,
                  ref_map=_json_arg(args.ref_map) or "auto", net_map=_json_arg(args.net_map),
                  join_nets=_csv(args.join_nets), drop_refs=_csv(args.drop_refs),
                  private_nets="keep" if args.keep_nets else "prefix",
                  keep_placement=args.keep_placement)
    design = dp.relocate_libraries(res.design, base_path.parent, out.parent)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(design, indent=1, ensure_ascii=False) + "\n", encoding="utf-8",
                   newline="\n")
    return res, out


def main(argv: list[str] | None = None) -> int:
    """Exit 0: written; 1: written, with problems to review; 2: usage/IO error."""
    ap = argparse.ArgumentParser(description="Compose a base netlist.json with a block "
                                 "(refs renumbered, nets joined or prefixed, libraries merged).")
    ap.add_argument("base", help="the base netlist.json (its source/board/rebuild are kept)")
    ap.add_argument("block", help="the block netlist.json")
    ap.add_argument("out", help="the composite netlist.json to write")
    ap.add_argument("--prefix", required=True, help="block name and private-net prefix")
    ap.add_argument("--net-map", help="JSON {block net: net here}, inline or a file")
    ap.add_argument("--ref-map", help="JSON {block ref: new ref}, inline or a file")
    ap.add_argument("--join-nets", default=",".join(DEFAULT_JOIN_NETS),
                    help="comma-separated nets joined by name (default GND)")
    ap.add_argument("--drop-refs", default="", help="comma-separated BASE refs to remove")
    ap.add_argument("--keep-nets", action="store_true", help="keep private block net names")
    ap.add_argument("--keep-placement", action="store_true", help="keep block at/x/y")
    args = ap.parse_args(argv)
    try:
        res, out = _run(args)
    except (OSError, ValueError, TypeError) as e:     # json.JSONDecodeError is a ValueError
        print(f"design_blocks: {e}", file=sys.stderr)
        return 2
    print(json.dumps({"ok": res.ok, "out": str(out), "ref_map": res.ref_map,
                      "net_map": res.net_map, "problems": list(res.problems)}, indent=1))
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
