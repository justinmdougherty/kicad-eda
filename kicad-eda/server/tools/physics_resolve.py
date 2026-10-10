#!/usr/bin/env python3
"""The physics resolver: <netlist stem>.stackup.json and the managed <board stem>.kicad_dru
(ADR D7.4, wave-2a sections; task D7-13a).

resolve_design(design, netlist_path, *, board_path, fd, budget, cache) -> the artifact
(schema kicad-eda/physical@1), built by the ordered SECTION_BUILDERS:
  stackup          stackup_model.resolve(...).to_dict() (None on an ST-E1..E8 error)
  lines            impedance.resolve_impedance per net and allowed layer ({} without a
                   stackup); each record without its decision_drafts (they are collected)
  dsn              {signal_layers (signal + mixed), plane_nets {net: [plane layers]},
                   unused_layers, net_layers {net: {primary, window, widths {layer: mm}}}}:
                   every allowed layer of each impedance net (dsn_layers narrows them), the
                   partner of a diff pair included
  dru              {path (relative to the netlist folder), sha256 of the managed block, rules
                   (canary included), families {R8, R7, R6}} or {path, error}
  decision_drafts  decisions.append_decision fields without id / ts, from every section
  warnings, errors every section's, as text
Builders are (Context) -> Built; a builder sees the sections before it (Context.section).
The collectors (decision_drafts, warnings, errors) are evaluated after every other builder,
so a section appended later (D7-13b's rf) still contributes its drafts, warnings and errors.
dru_rules() is the one place the DRU rule list is made (D7-13b merges its RF rules there).

design_digest: sha256 of the physical keys (stackup, copper_layers, planes, net_specs, rf,
layer_policy, mode, preroutes, emc, the ordered net names, which choose the default plane
rail, and per component ref / footprint / side / at / x / y / rot / clock_hz, order-free).
data_version: sha256 of the materials, fabs and tline_fallback data versions.

Byte-stable (critique E14): no timestamps, ids or run times; FD seconds and solves go to the
write_artifact result, never the artifact; artifact_text() is the one serialisation.

write_artifact(netlist_path, design=None, *, board_path, fd, budget) -> {ok, path, dru_path,
dru, decisions_written, decisions, fd, changed, previous, design_digest, data_version,
warnings, errors}: FD results of the previous artifact are reused by cross-section hash
(impedance.fd_cache); the managed rules go to <board stem>.kicad_dru (dru_writer.write;
default board <netlist stem>.kicad_pcb, the harness's); drafts not in the previous artifact
are appended to <netlist stem>.decisions.jsonl (decisions.append_decision), so a draft is
recorded once; the artifact is rewritten only when its bytes change.

is_fresh / staleness(artifact, design, *, board_path): stale when the design digest or data
version differ, or (board_path given) the board's <stem>.kicad_dru is missing or its managed
block differs from the artifact's (the block hash also covers a mirror of the artifact).

CLI: physics_resolve.py <netlist> [--board <pcb>] [--fd off|auto|force]: the write_artifact
result as JSON; exit 0, 1 with design errors, 2 for an unusable netlist or argument.
fd 'auto' (default) uses fieldsolve2d when numpy/scipy are present, within
KICAD_EDA_FD_BUDGET_S (FdBudget.from_env); 'off' is stdlib only. Never mutates `design`.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import decisions
import dru_writer
import fab_presets
import impedance
import materials as mat
import stackup_model
from tline import fallback

SCHEMA = "kicad-eda/physical@1"
ARTIFACT_SUFFIX = ".stackup.json"
NETLIST_SUFFIX = ".json"
BOARD_SUFFIX = dru_writer.BOARD_SUFFIX
FD_MODES = impedance.FD_MODES
DEFAULT_FD = "auto"
DIGEST_KEYS = ("stackup", "copper_layers", "planes", "net_specs", "rf", "layer_policy", "mode",
               "preroutes", "emc")
COMPONENT_KEYS = ("footprint", "side", "at", "x", "y", "rot", "clock_hz")
COLLECTORS = ("decision_drafts", "warnings", "errors")
FAMILY_NAMES = ("R8", "R7", "R6")
DRAFT_STAMP = {"id": "dec-000000000000", "ts": "2000-01-01T00:00:00+00:00"}  # validation only
EXIT_OK, EXIT_ERRORS, EXIT_USAGE = 0, 1, 2


# ---------------------------------------------------------------- data
@dataclass(frozen=True)
class DataSet:
    """The data files one resolution reads (D7.3): materials, fabs, the tline fallback."""

    materials: mat.MaterialLibrary
    fabs: fab_presets.FabSet
    fallback_digest: str

    @property
    def version(self) -> str:
        parts = {"fabs": self.fabs.data_version, "materials": self.materials.data_version,
                 "tline_fallback": self.fallback_digest}
        return _sha(_canonical(parts))


def load_data(materials: mat.MaterialLibrary | None = None,
              fabs: fab_presets.FabSet | None = None) -> DataSet:
    """The shipped data (or the given library / fab set) and the fallback table digest."""
    return DataSet(materials if materials is not None else mat.load_library(),
                   fabs if fabs is not None else fab_presets.load_fabs(),
                   fallback.default_table().digest)


def data_version(materials: mat.MaterialLibrary | None = None,
                 fabs: fab_presets.FabSet | None = None) -> str:
    """sha256 over the materials, fabs and tline_fallback data versions (the route cache key)."""
    return load_data(materials, fabs).version


# ---------------------------------------------------------------- small helpers
def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      default=repr).encode("ascii")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dedupe(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _issue_text(issue: Mapping[str, Any]) -> str:
    return f"{issue['id']} at {issue['path']}: {issue['message']}"


def _relative(path: Path, base: Path) -> str:
    """`path` relative to `base` with / separators (absolute across drives)."""
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(base))
    except ValueError:
        return Path(os.path.abspath(path)).as_posix()
    return Path(rel).as_posix()


def artifact_path(netlist_path: str | os.PathLike[str]) -> Path:
    """<netlist stem>.stackup.json beside the netlist."""
    path = Path(netlist_path)
    stem = path.with_suffix("") if path.suffix == NETLIST_SUFFIX else path
    return stem.with_name(stem.name + ARTIFACT_SUFFIX)


def default_board(netlist_path: str | os.PathLike[str]) -> Path:
    """The board the harness builds for a netlist: <netlist stem>.kicad_pcb."""
    path = Path(netlist_path)
    stem = path.with_suffix("") if path.suffix == NETLIST_SUFFIX else path
    return stem.with_name(stem.name + BOARD_SUFFIX)


def make_budget(fd: str, budget: Any = None) -> Any:
    """The FD budget of a run: None for fd 'off', the given one, else FdBudget.from_env()."""
    if fd not in FD_MODES:
        raise ValueError(f"fd must be one of {FD_MODES}, got {fd!r}")
    if fd == "off":
        return None
    if budget is not None:
        return budget
    import fieldsolve2d                 # numpy/scipy are imported lazily inside it
    return fieldsolve2d.FdBudget.from_env()


# ---------------------------------------------------------------- digest
def _components(design: Mapping[str, Any]) -> list[dict[str, Any]]:
    comps = design.get("components")
    rows = [{"ref": c.get("ref"), **{k: c[k] for k in COMPONENT_KEYS if k in c}}
            for c in (comps if isinstance(comps, list) else []) if isinstance(c, Mapping)]
    return sorted(rows, key=_canonical)


def design_digest(design: Mapping[str, Any]) -> str:
    """sha256 of the design's physical keys (module docstring); order of components free."""
    if not isinstance(design, Mapping):
        raise TypeError(f"design must be a netlist object, got {type(design).__name__}")
    nets = design.get("nets")
    payload = {**{k: design.get(k) for k in DIGEST_KEYS},
               "net_names": [n for n in (nets if isinstance(nets, (Mapping, list)) else ())
                             if isinstance(n, str)],
               "components": _components(design)}
    return _sha(_canonical(payload))


# ---------------------------------------------------------------- builders
@dataclass(frozen=True)
class Built:
    """One section: its value and what it contributes to the collected sections."""

    value: Any
    drafts: tuple[Mapping[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class Context:
    """What every builder reads; `sections` holds the sections built so far."""

    design: Mapping[str, Any]
    netlist_path: Path
    board_path: Path
    fd: str
    budget: Any
    cache: Mapping[str, Mapping[str, Any]]
    data: DataSet
    rs: Any
    issues: tuple[Any, ...]
    sections: Mapping[str, Built] = field(default_factory=lambda: MappingProxyType({}))

    def section(self, name: str) -> Any:
        """The value of an earlier section (None when it is not built)."""
        built = self.sections.get(name)
        return None if built is None else built.value

    def with_section(self, name: str, built: Built) -> Context:
        return replace(self, sections=MappingProxyType({**self.sections, name: built}))


Builder = Callable[[Context], Built]


def _build_stackup(ctx: Context) -> Built:
    errors = tuple(str(i) for i in ctx.issues if i.severity == "error")
    warnings = tuple(str(i) for i in ctx.issues if i.severity != "error")
    if ctx.rs is None:
        return Built(None, (), warnings, errors)
    return Built(ctx.rs.to_dict(), tuple(ctx.rs.draft_dicts()), warnings, errors)


def _line_notes(net: str, rec: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """(warnings, errors) of one impedance record, as text."""
    issues = rec.get("issues") or []
    errors = [_issue_text(i) for i in issues if i["severity"] == "error"]
    warnings = [_issue_text(i) for i in issues if i["severity"] != "error"]
    labelled = (f"{net} ", f"{net}:")             # tline labels most warnings '<net> on <layer>'
    warnings += [w if w.startswith(labelled) else f"{net}: {w}" for w in rec.get("warnings") or []]
    return warnings, errors


def _build_lines(ctx: Context) -> Built:
    if ctx.rs is None:
        return Built({})
    records = impedance.resolve_impedance(
        ctx.design, fd=ctx.fd, budget=ctx.budget, cache=ctx.cache, errors="record",
        materials=ctx.data.materials, fabs=ctx.data.fabs)
    lines, drafts, warnings, errors = {}, [], [], []
    for net, rec in records.items():
        lines[net] = {k: v for k, v in rec.items() if k != "decision_drafts"}
        drafts += rec.get("decision_drafts") or []
        more_w, more_e = _line_notes(net, rec)
        warnings, errors = warnings + more_w, errors + more_e
    return Built(lines, tuple(drafts), tuple(warnings), tuple(errors))


def _net_entry(rec: Mapping[str, Any]) -> dict[str, Any] | None:
    layers = rec.get("layers")
    if not isinstance(layers, Mapping) or not layers or rec.get("primary_layer") not in layers:
        return None
    return {"primary": rec["primary_layer"], "window": list(rec["width_window"]),
            "widths": {layer: line["width"] for layer, line in layers.items()}}


def _net_layers(design: Mapping[str, Any], lines: Mapping[str, Any]) -> dict[str, Any]:
    """{net: entry} of every resolved line, plus each diff line's partner (sorted by net)."""
    pairs, out = dru_writer.pairs_of(design), {}
    for net, rec in lines.items():
        entry = _net_entry(rec)
        if entry is None:
            continue
        out[net] = entry
        partner = pairs.get(net)
        if rec.get("kind") == "diff" and partner and partner not in lines:
            out.setdefault(partner, entry)
    return {net: out[net] for net in sorted(out)}


def _build_dsn(ctx: Context) -> Built:
    if ctx.rs is None:
        return Built(None)
    plane_nets: dict[str, list[str]] = {}
    for c in ctx.rs.copper:
        if c.role == "plane" and c.net:
            plane_nets.setdefault(c.net, []).append(c.name)
    net_layers = _net_layers(ctx.design, ctx.section("lines") or {})
    clash = sorted(set(net_layers) & set(plane_nets))
    errors = tuple(f"net {net} has an impedance target but is the plane net of "
                   f"{', '.join(plane_nets[net])}: a plane net is poured, not routed as a "
                   "line; remove its impedance / diff target" for net in clash)
    return Built({"signal_layers": list(ctx.rs.routable_layers()), "plane_nets": plane_nets,
                  "unused_layers": [c.name for c in ctx.rs.copper if c.role == "unused"],
                  "net_layers": {n: e for n, e in net_layers.items() if n not in clash}},
                 errors=errors)


def dru_rules(design: Mapping[str, Any], stackup: Any,
              lines: Mapping[str, Any] | None) -> list[dru_writer.Rule]:
    """The managed rules (the one site: D7-13b merges the RF families here): the 2a base
    families R8 / R7 / R6 of dru_writer. ValueError for a rule KiCad cannot take (ST-E11)."""
    return dru_writer.base_rules(stackup, lines or {}, pairs=dru_writer.pairs_of(design))


def _families(rules: Sequence[dru_writer.Rule]) -> dict[str, int]:
    found = [dru_writer.family_of(r) for r in rules]
    return {name: found.count(name) for name in FAMILY_NAMES}


def _build_dru(ctx: Context) -> Built:
    if ctx.rs is None:
        return Built(None)
    path = _relative(dru_writer.dru_path_for(ctx.board_path), ctx.netlist_path.parent)
    try:
        rules = dru_rules(ctx.design, ctx.section("stackup"), ctx.section("lines"))
        block = dru_writer.render_block(rules)
    except ValueError as exc:
        return Built({"path": path, "error": str(exc)}, errors=(f"DRU: {exc}",))
    return Built({"path": path, "sha256": _sha(block.encode("utf-8")), "rules": len(rules) + 1,
                  "families": _families(rules)})


def _contributions(ctx: Context, attr: str) -> list[Any]:
    return [x for name, built in ctx.sections.items() if name not in COLLECTORS
            for x in getattr(built, attr)]


def _draft_problems(draft: Any) -> list[str]:
    if not isinstance(draft, Mapping):
        return [f"a decision draft must be an object, got {type(draft).__name__}"]
    return decisions.validate_decision({**draft, **DRAFT_STAMP})


def _plain_draft(draft: Mapping[str, Any]) -> dict[str, Any]:
    return {**draft, "alternatives": list(draft.get("alternatives") or [])}


def _collect_drafts(ctx: Context) -> Built:
    seen: set[bytes] = set()
    out = []
    for draft in _contributions(ctx, "drafts"):
        if _draft_problems(draft):
            continue
        plain = _plain_draft(draft)
        key = _canonical(plain)
        if key not in seen:
            seen.add(key)
            out.append(plain)
    return Built(out)


def _collect_warnings(ctx: Context) -> Built:
    return Built(_dedupe(_contributions(ctx, "warnings")))


def _collect_errors(ctx: Context) -> Built:
    bad = [f"invalid decision draft ({'; '.join(p)}): {d!r}"
           for d in _contributions(ctx, "drafts") if (p := _draft_problems(d))]
    return Built(_dedupe([*_contributions(ctx, "errors"), *bad]))


SECTION_BUILDERS: tuple[tuple[str, Builder], ...] = (
    ("stackup", _build_stackup),
    ("lines", _build_lines),
    ("dsn", _build_dsn),
    ("dru", _build_dru),
    ("decision_drafts", _collect_drafts),
    ("warnings", _collect_warnings),
    ("errors", _collect_errors),
)


# ---------------------------------------------------------------- resolve
def resolve_design(design: Mapping[str, Any], netlist_path: str | os.PathLike[str], *,
                   board_path: str | os.PathLike[str] | None = None, fd: str = DEFAULT_FD,
                   budget: Any = None, cache: Mapping[str, Mapping[str, Any]] | None = None,
                   materials: mat.MaterialLibrary | None = None,
                   fabs: fab_presets.FabSet | None = None) -> dict[str, Any]:
    """The artifact of `design` (module docstring). `board_path` defaults to the netlist
    stem's board; `cache` holds FD entries by cross-section hash (impedance.fd_cache of a
    previous artifact's lines). Pure apart from the FD budget it charges."""
    if not isinstance(design, Mapping):
        raise TypeError(f"design must be a netlist object, got {type(design).__name__}")
    net = Path(netlist_path)
    board = Path(board_path) if board_path is not None else default_board(net)
    dru_writer.dru_path_for(board)                       # a .kicad_pcb, else ValueError
    data = load_data(materials, fabs)
    rs, issues = stackup_model.resolve(design, materials=data.materials, fabs=data.fabs)
    ctx = Context(design, net, board, fd, make_budget(fd, budget), dict(cache or {}), data,
                  rs, tuple(issues))
    for name, builder in SECTION_BUILDERS:
        if name not in COLLECTORS:
            ctx = ctx.with_section(name, builder(ctx))
    for name, builder in SECTION_BUILDERS:
        if name in COLLECTORS:
            ctx = ctx.with_section(name, builder(ctx))
    head = {"schema": SCHEMA, "design_digest": design_digest(design),
            "data_version": data.version}
    return {**head, **{name: ctx.section(name) for name, _ in SECTION_BUILDERS}}


def artifact_text(artifact: Mapping[str, Any]) -> str:
    """The artifact file text: the one serialisation (ASCII JSON, LF, trailing newline)."""
    return json.dumps(artifact, indent=1, ensure_ascii=True, allow_nan=False) + "\n"


def read_artifact(path: str | os.PathLike[str]) -> dict[str, Any]:
    """A physical@1 artifact; ValueError when it is not one, OSError when unreadable."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"{path} is not JSON: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ValueError(f"{path} is not a {SCHEMA} artifact")
    return doc


# ---------------------------------------------------------------- freshness
def _managed_block(text: str) -> str | None:
    """The managed block of a rules file as dru_writer wrote it (None when absent)."""
    start = text.find(dru_writer.BEGIN_MARKER)
    end = text.find(dru_writer.END_MARKER, start + 1) if start >= 0 else -1
    if start < 0 or end < 0 or (start > 0 and text[start - 1] not in "\r\n"):
        return None
    stop = text.find("\n", end)
    return text[start:] if stop < 0 else text[start:stop + 1]


def _dru_staleness(art: Mapping[str, Any], board: Path) -> str | None:
    """Does the board's own rules file hold this artifact's managed block? (The block hash
    is the proof: it holds wherever the artifact, or a mirror of it, lives.)"""
    dru, want = art.get("dru"), dru_writer.dru_path_for(board)
    if not isinstance(dru, Mapping) or "sha256" not in dru:
        return None
    try:
        block = _managed_block(want.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return f"the rules file {want} is missing"
    except (OSError, UnicodeError) as exc:
        return f"the rules file {want} is unreadable ({exc})"
    if block is None or _sha(block.encode("utf-8")) != dru["sha256"]:
        return (f"the managed block of {want} is missing or differs from the artifact's "
                f"(resolved for {dru.get('path')})")
    return None


def staleness(artifact: Mapping[str, Any] | str | os.PathLike[str],
              design: Mapping[str, Any], *, board_path: str | os.PathLike[str] | None = None,
              materials: mat.MaterialLibrary | None = None,
              fabs: fab_presets.FabSet | None = None) -> str | None:
    """Why the artifact (a dict or its path) is stale for `design` (module docstring), or
    None when it is fresh. With `board_path`, the board's <stem>.kicad_dru must also hold the
    artifact's managed block."""
    try:
        art = artifact if isinstance(artifact, Mapping) else read_artifact(artifact)
    except FileNotFoundError:
        return f"no artifact at {artifact}"
    except (OSError, ValueError) as exc:
        return f"the artifact is unreadable ({exc})"
    if art.get("schema") != SCHEMA:
        return f"not a {SCHEMA} artifact"
    if art.get("design_digest") != design_digest(design):
        return "the design digest changed (stackup, net_specs, planes, parts ... were edited)"
    if art.get("data_version") != data_version(materials, fabs):
        return "the data version changed (materials, fab presets or the tline fallback data)"
    return None if board_path is None else _dru_staleness(art, Path(board_path))


def is_fresh(artifact: Mapping[str, Any] | str | os.PathLike[str],
             design: Mapping[str, Any], **kwargs: Any) -> bool:
    """True when staleness(...) finds nothing (same keyword arguments)."""
    return staleness(artifact, design, **kwargs) is None


# ---------------------------------------------------------------- write
def _load_netlist(path: Path) -> dict[str, Any]:
    try:
        design = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"netlist not found: {path}") from exc
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"{path} is not a JSON netlist: {exc}") from exc
    if not isinstance(design, dict):
        raise ValueError(f"{path} is not a JSON object netlist (got {type(design).__name__})")
    return design


def _previous(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """(the previous artifact, None) or (None, why there is none usable)."""
    if not path.is_file():
        return None, None
    try:
        return read_artifact(path), None
    except (OSError, ValueError) as exc:
        return None, f"the previous artifact was unreadable and is replaced ({exc})"


def _cache_of(previous: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    lines = (previous or {}).get("lines")
    if not isinstance(lines, Mapping):
        return {}
    try:
        return impedance.fd_cache(lines)
    except (AttributeError, TypeError):
        return {}


def cached_fd(netlist_path: str | os.PathLike[str]) -> dict[str, dict[str, Any]]:
    """The FD cache entries of the netlist's current artifact ({} when there is none)."""
    return _cache_of(_previous(artifact_path(netlist_path))[0])


def _fd_report(fd: str, budget: Any, cache_in: Mapping[str, Any],
               lines: Mapping[str, Any]) -> dict[str, Any]:
    used = impedance.fd_cache(lines or {})
    backend = None
    if fd != "off":
        import fieldsolve2d
        backend = fieldsolve2d.available()
    return {"mode": fd, "backend": backend,
            "budget_s": None if budget is None else budget.seconds,
            "spent_s": 0.0 if budget is None else round(budget.spent, 3),
            "reused": sum(1 for k in used if k in cache_in),
            "computed": sum(1 for k in used if k not in cache_in)}


def _new_drafts(drafts: Sequence[Mapping[str, Any]],
                previous: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    """The drafts not recorded by the previous artifact (none when the set is unchanged)."""
    old = (previous or {}).get("decision_drafts")
    known = {_canonical(d) for d in (old if isinstance(old, list) else [])}
    return [d for d in drafts if _canonical(d) not in known]


def _record(netlist: Path, drafts: Sequence[Mapping[str, Any]]) -> tuple[list[str], list[str]]:
    """Append each draft to <stem>.decisions.jsonl: (ids written, errors)."""
    ids, errors = [], []
    for draft in drafts:
        try:
            ids.append(decisions.append_decision(netlist, **draft)["id"])
        except (decisions.DecisionError, OSError) as exc:
            errors.append(f"decision not recorded ({type(exc).__name__}: {exc})")
    return ids, errors


def _atomic_write(path: Path, data: bytes) -> bool:
    """Replace `path` with `data` unless it already holds those bytes; True when written."""
    with contextlib.suppress(OSError):
        if path.read_bytes() == data:
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return True


def _write_dru(board: Path, design: Mapping[str, Any],
               art: Mapping[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    dru = art.get("dru")
    if not isinstance(dru, Mapping) or "sha256" not in dru:
        return None, []
    try:
        return dru_writer.write(board, dru_rules(design, art["stackup"], art["lines"])), []
    except (OSError, ValueError) as exc:
        return None, [f"DRU not written to {dru_writer.dru_path_for(board)}: {exc}"]


def write_artifact(netlist_path: str | os.PathLike[str], design: Mapping[str, Any] | None = None,
                   *, board_path: str | os.PathLike[str] | None = None, fd: str = DEFAULT_FD,
                   budget: Any = None, materials: mat.MaterialLibrary | None = None,
                   fabs: fab_presets.FabSet | None = None) -> dict[str, Any]:
    """Resolve and write the artifact, the managed DRU and the new decisions (module
    docstring). `design` defaults to the netlist file's content. ValueError / OSError for an
    unusable netlist or board path (nothing is written then)."""
    net = Path(netlist_path)
    design = _load_netlist(net) if design is None else design
    board = Path(board_path) if board_path is not None else default_board(net)
    dru_path = dru_writer.dru_path_for(board)
    path = artifact_path(net)
    previous, note = _previous(path)
    cache, budget = _cache_of(previous), make_budget(fd, budget)
    art = resolve_design(design, net, board_path=board, fd=fd, budget=budget, cache=cache,
                         materials=materials, fabs=fabs)
    dru, dru_errors = _write_dru(board, design, art)
    ids, dec_errors = _record(net, _new_drafts(art["decision_drafts"], previous))
    changed = _atomic_write(path, artifact_text(art).encode("ascii"))
    errors = [*art["errors"], *dru_errors, *dec_errors]
    return {"ok": not errors, "path": str(path), "dru_path": str(dru_path), "dru": dru,
            "decisions_written": len(ids), "decisions": ids,
            "fd": _fd_report(fd, budget, cache, art["lines"]), "changed": changed,
            "previous": note, "design_digest": art["design_digest"],
            "data_version": art["data_version"], "warnings": art["warnings"], "errors": errors}


# ---------------------------------------------------------------- CLI
def main(argv: Sequence[str] | None = None) -> int:
    """physics_resolve.py <netlist> [--board <pcb>] [--fd off|auto|force] (module docstring)."""
    parser = argparse.ArgumentParser(
        description="Write <netlist stem>.stackup.json and the managed <board stem>.kicad_dru "
                    "(ADR D7.4).")
    parser.add_argument("netlist", help="the design's netlist.json")
    parser.add_argument("--board", help="the board (.kicad_pcb) whose rules file to write "
                                        "(default: <netlist stem>.kicad_pcb)")
    parser.add_argument("--fd", choices=FD_MODES, default=DEFAULT_FD,
                        help="FD verification tier (default auto)")
    args = parser.parse_args(argv)
    try:
        out = write_artifact(args.netlist, board_path=args.board, fd=args.fd)
    except (OSError, ValueError, TypeError) as exc:
        print(f"physics_resolve: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(json.dumps(out, indent=1))
    return EXIT_OK if out["ok"] else EXIT_ERRORS


if __name__ == "__main__":
    raise SystemExit(main())
