#!/usr/bin/env python3
"""The D0 footprint library of a KiCad project import (ROADMAP D0 / D4).

A rebuild loads its footprints with the oldest supported pcbnew (D0: KiCad
9 locally, ``(version 20241229)``), so ``provide_footprints`` writes
``libs/<name>_imported.pretty`` in that dialect by the first route that
works (recorded as ``route``; every route tried before it, with its reason,
under ``routes_tried``):

``pcbnew9``
    a board in KiCad 9's dialect: its own footprints, saved by the oldest
    pcbnew >= 9.0 (kicad_import_fplib, unchanged: in a KiCad 10 only image
    that is pcbnew 10, whose dialect is reported).
``pcbnew10+downgrade``
    a newer board that a pcbnew >= 10 installed here reads (KiCad 10.0
    reads up to 20260206, the 10.99 nightly anything): its own footprints
    saved by that pcbnew (instance fields stripped from the text, since
    pcbnew 10 cannot remove a field safely), then kicad_dialect.downgrade_footprint.
``importer_library``
    ``sources``: the KiCad 11 importer's ``<stem>-import-fps.pretty``
    (foreign_import's importer_footprint_library). The item named like each
    board footprint (a directory named like its library nickname first),
    downgraded; each item's numbered pads must be the board instance's.
``project_tables``
    the source project's own fp-lib-table libraries
    (kicad_import_fplib.project_footprint_libs): the items the board uses,
    downgraded when newer, pads verified the same way.

Every route names, merges and installs through kicad_import_fplib.build_library
(footprint_identity, ``name_2`` variants), and points ``${KIPRJMOD}``-relative
(or plain relative) 3D models at the file inside the source project; a
``${KIPRJMOD}`` model whose file does not exist is dropped (``models_dropped``,
distinct paths; embedded and stock-variable models stay as they are). A written
library is proven by KiCad 9 (``k9_verified``: kicad_dialect.k9_check; None
without a KiCad 9 install). When no route works the entry keeps every
route's reason, the refused constructs and, for a board newer than KiCad 9's
dialect, ``kicad11_path_required``; a pcbnew that failed is raised again
then (a failure is never quiet), and is a warning when a later route worked.
"""
from __future__ import annotations

import os
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import kicad_dialect as kd  # noqa: E402
import kicad_import_fplib as kfp  # noqa: E402
import sexpr_read as sr  # noqa: E402
from kicad_import_pcb import BoardDesign, BoardFootprint  # noqa: E402
from kicad_import_sch import child_spans  # noqa: E402

ROUTES = ("pcbnew9", "pcbnew10+downgrade", "importer_library", "project_tables")
K9_PCB_FORMAT = kfp.K9_PCB_FORMAT
K10_PCB_FORMAT = 20260206                 # the newest board KiCad 10.0's pcbnew reads
NEWER_PCBNEW, NIGHTLY_PCBNEW = (10, 0, 0), (10, 99, 0)
LISTED = 6                                # names one reason lists
MODELS_LISTED = 20
_MODEL = re.compile(r'\(\s*model\s+("(?:[^"\\]|\\.)*"|[^\s()"]+)')
_KIPRJMOD = re.compile(r"^(?:\$\{KIPRJMOD\}|\$\(KIPRJMOD\))[\\/]*")
_ANY_VAR = re.compile(r"\$[{(]")
_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]+://")         # kicad-embed://, file://
_WINDOWS_ABS = re.compile(r"^[A-Za-z]:[\\/]")


class _NoRoute(Exception):
    """A route that does not apply or did not work: why, and what
    kicad_dialect refused on it."""

    def __init__(self, reason: str, refused: Sequence[Mapping[str, str]] = ()) -> None:
        super().__init__(reason)
        self.reason, self.refused = reason, tuple(refused)


@dataclass(frozen=True)
class _Context:
    board_path: Path
    board: BoardDesign
    nick: str
    out_pretty: Path
    sources: tuple[Path, ...]
    keep: frozenset[int]
    kicad_python: str | None


@dataclass(frozen=True)
class _Built:
    library: kfp.FootprintLibrary
    info: Mapping[str, Any]


def verify(pretty: Path, kind: str = "fp") -> dict[str, Any]:
    """KiCad 9's own load check of a written library (kicad_dialect.k9_check)."""
    return kd.k9_check(pretty, kind)


def _listed(names: Sequence[str]) -> str:
    more = len(names) - LISTED
    return ", ".join(names[:LISTED]) + (f" and {more} more" if more > 0 else "")


# ----------------------------------------------------------------------------
# 3D models and pads
# ----------------------------------------------------------------------------
class ModelEdit(NamedTuple):
    text: str
    rewritten: tuple[str, ...]          # model paths now absolute inside the source project
    dropped: tuple[str, ...]            # model paths whose file does not exist


def _project_file(path: str, project: Path) -> tuple[Path | None, bool]:
    """(the file a project-relative model path names, whether it says
    ``${KIPRJMOD}``): a plain relative path counts too (KiCad tries the
    project first); None for anything else: absolute, another variable
    (``${KICAD9_3DMODEL_DIR}``), an embedded ``kicad-embed://`` file."""
    rest = _KIPRJMOD.sub("", path)
    if rest != path:
        return project / rest, True
    if (_ANY_VAR.search(path) or _URI.match(path) or path.startswith(("/", "\\"))
            or _WINDOWS_ABS.match(path)):
        return None, False
    return project / path, False


def rewrite_models(text: str, project: Path) -> ModelEdit:
    """A footprint whose project-relative ``(model ...)`` paths point at the
    file inside ``project`` (absolute, '/' separators) when it exists. A
    missing ``${KIPRJMOD}`` model is dropped (with the whitespace before it);
    a missing plain relative one stays (KiCad's 3D search paths may hold it)."""
    root = text.index("(")
    edits: list[tuple[int, int, str]] = []
    rewritten: list[str] = []
    dropped: list[str] = []
    for head, a, b in child_spans(text, root, len(text)):
        m = _MODEL.match(text, a) if head == "model" else None
        original = kd.unquote(m.group(1)) if m else ""
        target, explicit = _project_file(original, project) if m else (None, False)
        if target is not None and target.is_file():
            full = Path(os.path.abspath(target)).as_posix()
            edits.append((m.start(1), m.end(1),
                          '"' + full.replace("\\", "\\\\").replace('"', '\\"') + '"'))
            rewritten.append(original)
        elif target is not None and explicit:
            edits.append((len(text[:a].rstrip(" \t\r\n")), b, ""))
            dropped.append(original)
    out, pos = [], 0
    for a, b, new in edits:
        out += [text[pos:a], new]
        pos = b
    return ModelEdit("".join(out + [text[pos:]]), tuple(rewritten), tuple(dropped))


def numbered_pads(text: str) -> Counter[str]:
    """The pad numbers of a library footprint as kicad_import_pcb reads a
    board instance's (sexpr_read.pad_number: no NPTH or aperture pads)."""
    root = text.index("(")
    out: Counter[str] = Counter()
    for head, a, b in child_spans(text, root, len(text)):
        if head == "pad" and (number := sr.pad_number(sr.parse(sr.quote_bare_keys(text[a:b])))):
            out[number] += 1
    return out


def _board_pads(fp: BoardFootprint) -> Counter[str]:
    return Counter(p.number for p in fp.pads)


# ----------------------------------------------------------------------------
# Downgrade
# ----------------------------------------------------------------------------
def _version(text: str) -> int | None:
    m = sr.VERSION_RE.search(text, 0, 512)
    return int(m.group(1)) if m else None


def _downgrade(texts: Mapping[str, str], route: str
               ) -> tuple[dict[str, str], list[dict[str, str]], dict[str, dict[str, int]]]:
    """({item: KiCad 9 text}, refusals, {dropped, rewritten} counts) of
    library texts by item name; texts not newer than KiCad 9's are kept."""
    out: dict[str, str] = {}
    refused: list[dict[str, str]] = []
    dropped: Counter[str] = Counter()
    rewritten: Counter[str] = Counter()
    for item, text in texts.items():
        if (_version(text) or 0) <= K9_PCB_FORMAT:
            out[item] = text
            continue
        res = kd.downgrade_footprint(text)
        if not res.ok:
            refused.append({"route": route, "item": item, "construct": res.construct or "",
                            "reason": res.refused or ""})
            continue
        out[item] = res.text
        dropped.update({c.rule: c.count for c in res.dropped})
        rewritten.update({c.rule: c.count for c in res.rewritten})
    return out, refused, {"dropped": dict(dropped), "rewritten": dict(rewritten)}


def _refusal(refused: Sequence[Mapping[str, str]]) -> _NoRoute:
    items = sorted({r["item"] for r in refused})
    constructs = sorted({r["construct"] for r in refused})
    return _NoRoute(f"kicad_dialect refused {len(items)} footprint(s) ({_listed(items)}): "
                    f"{', '.join(constructs)} has no KiCad 9 form", refused)


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
def choose_newer_install(board_format: int, installs: Iterable[Any]) -> Any | None:
    """The oldest install >= 10.0 whose bundled Python imports pcbnew and
    whose pcbnew reads ``board_format`` (10.0: up to K10_PCB_FORMAT; newer
    boards need the 10.99 nightly's)."""
    need = NEWER_PCBNEW if board_format <= K10_PCB_FORMAT else NIGHTLY_PCBNEW
    able = [i for i in installs if i.python is not None and tuple(i.version_tuple) >= need]
    return min(able, key=lambda i: tuple(i.version_tuple)) if able else None


def _newer_python(board_format: int) -> str:
    import kicad_env as env
    found = env.discover()
    install = choose_newer_install(board_format, found)
    if install is not None:
        return str(install.python)
    need = "10.0" if board_format <= K10_PCB_FORMAT else "10.99"
    seen = "; ".join(f"KiCad {i.version}: " + ("pcbnew ok" if i.python else
                                               getattr(i, "python_error", None) or "no pcbnew")
                     for i in found)
    raise _NoRoute(f"format {board_format} needs a KiCad >= {need} whose bundled Python imports "
                   f"pcbnew ({seen or 'no KiCad found'})")


def _route_pcbnew9(ctx: _Context) -> _Built:
    fmt = ctx.board.format_version
    if fmt is not None and fmt > K9_PCB_FORMAT:
        raise _NoRoute(f"the board is format {fmt}, newer than KiCad 9's {K9_PCB_FORMAT}")
    python = ctx.kicad_python
    if python is None:
        python, why = kfp.swig_python(fmt)
        if python is None:
            raise _NoRoute(why)
    saved = kfp.save_footprints(ctx.board_path, ctx.board.footprints, python,
                                keep_instance_attrs=ctx.keep)
    return _finish(ctx, saved.items, pcbnew=saved.pcbnew, warnings=saved.warnings,
                   info={"python": python})


def _route_pcbnew10(ctx: _Context) -> _Built:
    fmt = ctx.board.format_version
    if fmt is None or fmt <= K9_PCB_FORMAT:
        raise _NoRoute("the board is in KiCad 9's dialect (route pcbnew9)")
    python = ctx.kicad_python or _newer_python(fmt)
    saved = kfp.save_footprints(ctx.board_path, ctx.board.footprints, python,
                                keep_instance_attrs=ctx.keep)
    keys = {f"{fp.index}": fp for fp, _text in saved.items}
    texts, refused, changes = _downgrade({f"{fp.index}": t for fp, t in saved.items},
                                         "pcbnew10+downgrade")
    if refused:             # one row per footprint name and construct
        rows: dict[tuple[str, str], dict[str, str]] = {}
        for r in refused:
            fp = keys[r["item"]]
            rows.setdefault((fp.item_name, r["construct"]),
                            {**r, "item": fp.item_name, "ref": fp.ref})
        raise _refusal(list(rows.values()))
    return _finish(ctx, [(keys[k], t) for k, t in texts.items()], pcbnew=saved.pcbnew,
                   warnings=saved.warnings, info={"python": python, "downgrade": changes})


def _find_item(fp: BoardFootprint, dirs: Sequence[Path]) -> Path | None:
    nick = fp.lib_id.rpartition(":")[0]
    for d in sorted(dirs, key=lambda d: d.stem != nick):          # the nickname's own first
        if (path := d / f"{fp.item_name}.kicad_mod").is_file():
            return path
    return None


def _route_importer(ctx: _Context) -> _Built:
    if not ctx.sources:
        raise _NoRoute("no footprint_sources given (the KiCad 11 importer's "
                       "<stem>-import-fps.pretty)")
    dirs = [d for d in ctx.sources if d.is_dir()]
    paths = {fp.index: _find_item(fp, dirs) for fp in ctx.board.footprints}
    missing = [f"{fp.ref or '?'} ({fp.lib_id})" for fp in ctx.board.footprints
               if paths[fp.index] is None]
    if missing:
        absent = [str(d) for d in ctx.sources if not d.is_dir()]
        raise _NoRoute(f"{len(missing)} board footprint(s) have no item in the footprint "
                       f"sources: {_listed(missing)}"
                       + (f"; not a directory: {', '.join(absent)}" if absent else ""))
    return _from_items(ctx, "importer_library", paths, [str(d) for d in ctx.sources])


def _route_tables(ctx: _Context) -> _Built:
    root = ctx.board_path.parent
    if not (root / "fp-lib-table").is_file():
        raise _NoRoute(f"the source project has no fp-lib-table ({root})")
    libs = kfp.project_footprint_libs(root, ctx.board)
    paths: dict[int, Path | None] = {}
    for fp in ctx.board.footprints:
        lib = libs.get(fp.lib_id.rpartition(":")[0])
        path = Path(lib) / f"{fp.item_name}.kicad_mod" if lib else None
        paths[fp.index] = path if path is not None and path.is_file() else None
    missing = [f"{fp.ref or '?'} ({fp.lib_id})" for fp in ctx.board.footprints
               if paths[fp.index] is None]
    if missing:
        raise _NoRoute(f"the source project's fp-lib-table libraries lack {len(missing)} board "
                       f"footprint(s): {_listed(missing)}")
    used = sorted({str(p.parent) for p in paths.values() if p is not None})
    return _from_items(ctx, "project_tables", paths, used)


def _from_items(ctx: _Context, route: str, paths: Mapping[int, Path | None],
                sources: list[str]) -> _Built:
    """Routes c/d: the library items in ``paths`` (one per board footprint)
    downgraded and checked pad for pad against the board."""
    files = {str(p): p for p in paths.values() if p is not None}
    texts, refused, changes = _downgrade(
        {key: p.read_text(encoding="utf-8") for key, p in files.items()}, route)
    if refused:
        raise _refusal([{**r, "item": files[r["item"]].stem} for r in refused])
    bad = []
    for fp in ctx.board.footprints:
        item = numbered_pads(texts[str(paths[fp.index])])
        if item != _board_pads(fp):
            bad.append(f"{fp.ref or '?'} ({fp.lib_id}): library pads "
                       f"{' '.join(sorted(item.elements(), key=sr.natural_key)) or 'none'}, board "
                       f"pads {' '.join(sorted(_board_pads(fp).elements(), key=sr.natural_key))}")
    if bad:
        raise _NoRoute(f"{len(bad)} library item(s) whose numbered pads are not the board "
                       f"instance's: {'; '.join(bad[:LISTED])}")
    items = [(fp, texts[str(paths[fp.index])]) for fp in ctx.board.footprints]
    return _finish(ctx, items, info={"sources": sources, "downgrade": changes})


# ----------------------------------------------------------------------------
# Writing and the report entry
# ----------------------------------------------------------------------------
def _finish(ctx: _Context, items: Iterable[tuple[BoardFootprint, str]], *, pcbnew: str = "",
            warnings: Iterable[str] = (), info: Mapping[str, Any]) -> _Built:
    """Models pointed into the source project, then the library installed."""
    edited = [(fp, rewrite_models(text, ctx.board_path.parent)) for fp, text in items]
    dropped = sorted({p for _fp, e in edited for p in e.dropped})
    rewritten = sorted({p for _fp, e in edited for p in e.rewritten})
    lib = kfp.build_library([(fp, e.text) for fp, e in edited], ctx.nick, ctx.out_pretty,
                            pcbnew=pcbnew, warnings=warnings)
    models = {"models_dropped": len(dropped), "models_rewritten": len(rewritten)}
    if dropped:
        models["models_missing"] = dropped[:MODELS_LISTED]
    return _Built(lib, {**info, **models})


def _k9(pretty: Path, lib: kfp.FootprintLibrary) -> dict[str, Any]:
    if not lib.files:
        return {"k9_verified": None}
    check = verify(pretty)
    summary = {k: check[k] for k in ("loaded", "compared", "reason") if k in check}
    summary.update({k: len(check[k]) for k in ("differing", "missing", "failed_items")
                    if k in check})
    summary["errors"] = list(check.get("errors", ()))[:3]
    return {"k9_verified": check.get("ok"), "k9_check": summary}


def _success(base: dict[str, Any], ctx: _Context, route: str, built: _Built,
             tried: list[dict[str, Any]]) -> dict[str, Any]:
    lib = built.library
    warnings = list(lib.warnings) + [f"route {t['route']} failed: {t['reason']}"
                                     for t in tried if t.get("error")]
    if (note := kfp.dialect_warning(lib)) is not None:
        warnings.append(note)
    return {**base, "extracted": True, "route": route, "footprints": len(lib.files),
            "variants": list(lib.variants), "pcbnew": lib.pcbnew, "format": lib.format,
            "d0_dialect": kfp.in_d0_dialect(lib.format), **_k9(ctx.out_pretty, lib),
            "refused": [], **built.info, "routes_tried": tried, "warnings": warnings}


def _failure(base: dict[str, Any], board: BoardDesign, tried: list[dict[str, Any]],
             refused: list[Mapping[str, str]]) -> dict[str, Any]:
    out = {**base, "reason": "; ".join(f"{t['route']}: {t['reason']}" for t in tried),
           "routes_tried": tried, "refused": [dict(r) for r in refused]}
    if (board.format_version or 0) > K9_PCB_FORMAT:
        out["kicad11_path_required"] = True
    return out


_ROUTE_STEPS: Mapping[str, Callable[[_Context], _Built]] = {
    "pcbnew9": _route_pcbnew9, "pcbnew10+downgrade": _route_pcbnew10,
    "importer_library": _route_importer, "project_tables": _route_tables}


def provide_footprints(board_path: str | Path, board: BoardDesign, nick: str,
                       out_pretty: str | Path, *, sources: Iterable[str | Path] = (),
                       keep_instance_attrs: Iterable[int] = (), kicad_python: str | None = None,
                       enabled: bool = True) -> tuple[kfp.FootprintLibrary | None, dict]:
    """``out_pretty`` (nickname ``nick``) holding every footprint of the
    board in KiCad 9's dialect, by the first of ROUTES that works (module
    doc), as (library | None, import-report entry). ``sources``: footprint
    libraries to take items from (the KiCad 11 importer's); ``kicad_python``:
    the pcbnew to extract with (default: chosen per route);
    ``keep_instance_attrs``: footprints whose DNP / BOM flags stay in their
    extracted copy; ``enabled`` False writes nothing. Raises the RuntimeError
    of a pcbnew that failed when no route works."""
    base: dict[str, Any] = {"file": str(out_pretty), "extracted": False, "route": None}
    if not enabled:
        return None, {**base, "reason": "footprint extraction was turned off"}
    ctx = _Context(Path(board_path), board, nick, Path(out_pretty),
                   tuple(Path(s) for s in sources), frozenset(keep_instance_attrs), kicad_python)
    tried: list[dict[str, Any]] = []
    refused: list[Mapping[str, str]] = []
    errors: list[RuntimeError] = []
    for route in ROUTES:
        try:
            built = _ROUTE_STEPS[route](ctx)
        except _NoRoute as e:
            tried.append({"route": route, "reason": e.reason})
            refused += e.refused
            continue
        except RuntimeError as e:
            tried.append({"route": route, "reason": str(e), "error": True})
            errors.append(e)
            continue
        return built.library, _success(base, ctx, route, built, tried)
    if errors:
        raise errors[0]
    return None, _failure(base, board, tried, refused)
