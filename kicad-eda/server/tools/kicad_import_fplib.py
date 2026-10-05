#!/usr/bin/env python3
"""Footprint library of the KiCad project importer (ROADMAP_DECISIONS D4).

A source board in KiCad 9's dialect (D0: ``(version 20241229)``) gives the
rebuild its own footprints: kicad_import_swig.py, run under the bundled
Python of the OLDEST KiCad >= 9.0 that imports pcbnew (``choose_swig_install``:
KiCad 9 locally, KiCad 10 in Docker/CI), saves every placed footprint as a
library footprint; this side strips what the copy still carries of its board
instance (strip_instance_fields: schematic fields, symbol path and sheet),
merges identical copies (footprint_identity: geometry, not item order or
instance noise) and installs ``<name>_imported.pretty``. Both pcbnew 9 and
pcbnew 10 group the KiCad 9 demos' footprints identically (complex_hierarchy,
video, vme-wren, stickhub, tiny_tapeout: checked). The dialect pcbnew wrote
is read back from the files and reported: KiCad 10's pcbnew writes
``20260206``, which KiCad 9 cannot load.

``too_long_for_kicad`` names library files KiCad cannot open on Windows:
KiCad 9's FootprintLoad reads a .kicad_mod whose full path has 259
characters and fails at 260 (checked), and has been seen failing a whole
``.pretty`` that holds such a file.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kicad_import_pcb import BoardDesign, BoardFootprint  # noqa: E402
from kicad_import_sch import child_spans, io_path  # noqa: E402

K9_PCB_FORMAT = 20241229            # KiCad 9's board / footprint dialect (D0)
MIN_SWIG_VERSION = (9, 0, 0)        # D0: the oldest supported pcbnew
NEWER_SWIG_VERSION = (10, 0, 0)     # the only pcbnew that reads a board newer than K9_PCB_FORMAT
SWIG_SCRIPT = _HERE / "kicad_import_swig.py"
EXTRACT_TIMEOUT_S = 600.0
KICAD_MAX_PATH = 259                # Win32 MAX_PATH (260) less the terminating NUL
_BAD_FILE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_FP_HEAD = re.compile(r'\(\s*footprint\s+"((?:[^"\\]|\\.)*)"')
_FP_ROOT = re.compile(r'\(\s*footprint\s+(?:"(?:[^"\\]|\\.)*"|[^\s()"]+)')   # name quoted or not
_FP_VERSION = re.compile(r"\(\s*version\s+(\d+)\s*\)")
_VALUE_PROP = re.compile(r'(\(\s*property\s+"Value"\s+)"(?:[^"\\]|\\.)*"')
_UUID = re.compile(r'\(\s*(?:uuid|tstamp)\s+(?:"[^"]*"|[^\s()]+)\s*\)')
_REF_OR_VALUE = re.compile(r'\(\s*property\s+"(?:Reference|Value)"')
# a property's name, quoted ("MPN") or not (ki_fp_filters: KiCad writes ki_ names bare)
_PROPERTY = re.compile(r'\(\s*property\s+(?:"((?:[^"\\]|\\.)*)"|([^\s()"]+))')
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")
IDENTITY_DECIMALS = 3               # footprint_identity compares geometry to 1 um
KEEP_FIELDS = frozenset({"Reference", "Value", "Footprint", "Datasheet", "Description"})
# where on the board the footprint sat: the symbol path and its sheet (KiCad 10's
# FootprintSave keeps sheetname / sheetfile in a library copy, KiCad 9's drops them)
INSTANCE_HEADS = frozenset({"path", "sheetname", "sheetfile"})


@dataclass(frozen=True)
class FootprintLibrary:
    """Result of extract_footprint_library: ``names`` maps a footprint's
    BoardFootprint.index to its library id ``nick:name``; ``variants`` lists
    names that differ from the board's own footprint name (two instances of
    one footprint whose geometry differs); ``format`` is the ``(version)``
    pcbnew wrote (None when the files carry none)."""
    names: Mapping[int, str]
    files: tuple[str, ...]
    variants: tuple[Mapping[str, Any], ...]
    warnings: tuple[str, ...]
    pcbnew: str
    format: int | None = None


# ----------------------------------------------------------------------------
# Which pcbnew
# ----------------------------------------------------------------------------
def choose_swig_install(board_format: int | None, installs: Iterable[Any] | None = None
                        ) -> Any | None:
    """The kicad_env.KiCadInstall whose pcbnew extracts the footprints: the
    OLDEST install >= 9.0 whose bundled Python imports pcbnew and whose
    release reads ``board_format`` (D0: SWIG work runs on the oldest
    supported pcbnew, so a KiCad 9 board gives KiCad 9 footprints even with
    KiCad 10 installed beside it). ``installs`` defaults to
    kicad_env.discover(). None when no install qualifies."""
    if installs is None:
        import kicad_env as env
        installs = env.discover()
    need = NEWER_SWIG_VERSION if (board_format or 0) > K9_PCB_FORMAT else MIN_SWIG_VERSION
    able = [i for i in installs if i.python is not None and tuple(i.version_tuple) >= need]
    return min(able, key=lambda i: tuple(i.version_tuple)) if able else None


def _swig_python(board_format: int | None) -> tuple[str | None, str]:
    """(bundled Python of choose_swig_install, why there is none)."""
    import kicad_env as env
    found = env.discover()
    install = choose_swig_install(board_format, found)
    if install is not None:
        return str(install.python), ""
    seen = "; ".join(f"KiCad {i.version}: " + ("pcbnew ok" if i.python else
                                               getattr(i, "python_error", None) or "no pcbnew")
                     for i in found)
    return None, (f"no KiCad >= {'.'.join(map(str, MIN_SWIG_VERSION))} whose bundled Python "
                  f"imports pcbnew ({seen or 'no KiCad found'})")


# ----------------------------------------------------------------------------
# Footprint identity and names
# ----------------------------------------------------------------------------
def _identity_item(text: str, head: str, a: int, b: int) -> str | None:
    """A top-level item of a footprint as footprint_identity compares it:
    None for the Reference / Value fields; a text without its position (KiCad
    moves and turns text per instance to keep it readable); UUIDs dropped,
    numbers rounded to 1 um, whitespace collapsed."""
    if head == "property" and _REF_OR_VALUE.match(text, a):
        return None
    cuts = ([(s, e) for h, s, e in child_spans(text, a, b) if h == "at"]
            if head in ("property", "fp_text") else [])
    kept, pos = [], a
    for s, e in cuts:
        kept.append(text[pos:s])
        pos = e
    kept.append(text[pos:b])
    return " ".join(_NUMBER.sub(_quantum, _UUID.sub("", "".join(kept))).split())


def _instance_item(head: str, text: str, start: int) -> bool:
    """Is the top-level child (``head`` at text[start]) of a saved footprint
    instance data rather than library footprint?"""
    if head in INSTANCE_HEADS:
        return True
    if head != "property":
        return False
    m = _PROPERTY.match(text, start)
    name = m.group(1) if m and m.group(1) is not None else (m.group(2) if m else None)
    return name not in KEEP_FIELDS


def strip_instance_fields(text: str) -> str:
    """A saved footprint without the data of the instance it was taken from,
    each item with the whitespace before it: every top-level property whose
    name (quoted or bare) is not in KEEP_FIELDS (the schematic fields KiCad
    copies onto a placed footprint: MPN, LCSC, Sim.*, and the symbol's
    ``ki_fp_filters``), and the INSTANCE_HEADS (symbol path, sheet name and
    file). Without this, pcbnew 10 copies of one footprint from two sheets
    differ (footprint_identity) and become ``name_2`` variants. Done on the
    text, the same for every pcbnew, because pcbnew 10 cannot remove a field
    safely (kicad_import_swig.library_copy)."""
    root = text.index("(")
    cuts = []
    for head, a, b in child_spans(text, root, len(text)):
        if _instance_item(head, text, a):
            start = len(text[:a].rstrip(" \t\r\n"))
            cuts.append((start, b))
    kept, pos = [], 0
    for a, b in cuts:
        kept.append(text[pos:a])
        pos = b
    kept.append(text[pos:])
    return "".join(kept)


def _quantum(m: re.Match) -> str:
    return f"{round(float(m.group()), IDENTITY_DECIMALS) + 0.0:.{IDENTITY_DECIMALS}f}"


def footprint_identity(text: str) -> str:
    """A saved library footprint reduced to what makes two instances the same
    part: the sorted multiset of its top-level items (see _identity_item:
    without the name, UUIDs, the Reference / Value fields and text
    positions, every number rounded to 1 um, since un-rotating a part placed
    at 45 degrees moves points by 1 nm), plus any bare atoms after the name.
    Sorted because pcbnew 9 writes one footprint's texts in an order that
    follows where each instance's texts sit; item order means nothing to
    KiCad."""
    root = text.index("(")
    head = _FP_ROOT.match(text, root)
    if head is None:
        raise ValueError(f"not a saved footprint: {text[root:root + 60]!r}")
    spans = child_spans(text, root, len(text))
    atoms = " ".join(text[head.end():spans[0][1] if spans else len(text)].split())
    items = (_identity_item(text, h, a, b) for h, a, b in spans)
    return " | ".join([atoms, *sorted(i for i in items if i is not None)])


def _canonical_first(fp: BoardFootprint) -> tuple:
    """Instances placed F-side at a multiple of 90 degrees come first: their
    library copy is exact, so the group keeps their coordinates."""
    return (fp.side != "F", fp.rot % 90.0 != 0.0, fp.index)


def safe_item_name(name: str) -> str:
    """A footprint name usable as a file name and a library id item."""
    out = _BAD_FILE_CHARS.sub("_", name).strip(" .")
    return out or "footprint"


def _assign_names(saved: list[tuple[BoardFootprint, str]]
                  ) -> tuple[dict[int, str], dict[str, str], list[dict]]:
    """Group identical footprints; name each group after its board footprint
    name (canonical instances first, see _canonical_first), later different
    geometries ``name_2``..."""
    by_identity: dict[str, str] = {}
    texts: dict[str, str] = {}
    names: dict[int, str] = {}
    variants: list[dict] = []
    for fp, text in sorted(saved, key=lambda item: _canonical_first(item[0])):
        ident = footprint_identity(text)
        if ident not in by_identity:
            base = safe_item_name(fp.item_name)
            name, n = base, 1
            while name in texts:
                n += 1
                name = f"{base}_{n}"
            by_identity[ident] = name
            texts[name] = text
            if name != fp.item_name:
                variants.append({"name": name, "board_footprint": fp.lib_id, "first_ref": fp.ref})
        names[fp.index] = by_identity[ident]
    return names, texts, variants


def written_format(texts: Iterable[str]) -> int | None:
    """The newest ``(version N)`` among saved footprints (None: none carry one)."""
    found = [int(m.group(1)) for t in texts if (m := _FP_VERSION.search(t, 0, 512))]
    return max(found) if found else None


# ----------------------------------------------------------------------------
# Paths KiCad can open
# ----------------------------------------------------------------------------
def too_long_for_kicad(paths: Iterable[str | Path], platform: str | None = None) -> list[str]:
    """The library files KiCad cannot open: on Windows, an absolute path
    longer than KICAD_MAX_PATH (KiCad opens library files without the
    ``\\\\?\\`` prefix; see the module doc). Empty elsewhere."""
    if (platform or sys.platform) != "win32":
        return []
    out = []
    for p in paths:
        full = str(p)
        full = full[4:] if full.startswith("\\\\?\\") else os.path.abspath(full)
        if len(full) > KICAD_MAX_PATH:
            out.append(full)
    return out


# ----------------------------------------------------------------------------
# Extraction
# ----------------------------------------------------------------------------
def _run_swig(python: str, board_path: Path, plan: list[dict], work: Path,
              timeout_s: float) -> dict:
    plan_path = work / "plan.json"
    plan_path.write_text(json.dumps({"footprints": plan}), encoding="utf-8")
    out = work / "saved"
    try:
        r = subprocess.run([python, str(SWIG_SCRIPT), str(board_path), str(plan_path), str(out)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout_s, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"footprint extraction timed out after {timeout_s:g} s on "
                           f"{board_path}") from e
    except OSError as e:
        raise RuntimeError(f"cannot start KiCad's Python {python}: {e}") from e
    result = None
    for line in reversed(r.stdout.strip().splitlines()):
        if line.strip().startswith("{"):
            try:
                result = json.loads(line)
                break
            except ValueError:
                continue
    if result is None or not result.get("ok"):
        detail = (result or {}).get("error") or (result or {}).get("unmatched") \
            or (r.stdout + r.stderr)[-800:]
        raise RuntimeError(f"footprint extraction from {board_path.name} failed "
                           f"(exit {r.returncode}): {detail}")
    result["dir"] = out
    return result


def _install(texts: Mapping[str, str], out_pretty: Path) -> list[str]:
    """Write each footprint as <name>.kicad_mod into out_pretty, replacing the
    .kicad_mod files a previous import left there."""
    io_path(out_pretty).mkdir(parents=True, exist_ok=True)
    for old in io_path(out_pretty).glob("*.kicad_mod"):
        old.unlink()
    files = []
    for name, text in sorted(texts.items()):
        body = _FP_HEAD.sub(lambda m, n=name: f'(footprint "{n}"', text, count=1)
        body = _VALUE_PROP.sub(lambda m, n=name: f'{m.group(1)}"{n}"', body, count=1)
        path = out_pretty / f"{name}.kicad_mod"
        io_path(path).write_text(body, encoding="utf-8", newline="\n")
        files.append(os.path.abspath(path))
    return files


def extract_footprint_library(board_path: str | Path, board: BoardDesign,
                              footprints: Iterable[BoardFootprint], nick: str,
                              out_pretty: str | Path, kicad_python: str, *,
                              keep_instance_attrs: Iterable[int] = (),
                              timeout_s: float = EXTRACT_TIMEOUT_S) -> FootprintLibrary:
    """Save `footprints` of the board as the library `out_pretty` (nickname
    `nick`) with KiCad's own pcbnew (kicad_import_swig.py under
    `kicad_python`), one .kicad_mod per distinct geometry. Footprints whose
    index is in `keep_instance_attrs` keep their DNP / BOM flags (board-only
    parts: nothing else carries them). Raises ValueError for a board newer
    than KiCad 9's dialect (D0: use the KiCad 11 path) and RuntimeError when
    pcbnew fails."""
    board_path = Path(board_path)
    if board.format_version is not None and board.format_version > K9_PCB_FORMAT:
        raise ValueError(f"{board_path.name} is format {board.format_version}, newer than KiCad "
                         f"9's {K9_PCB_FORMAT}: its footprints are not extracted "
                         "(the board must go through the KiCad 11 path)")
    keep = set(keep_instance_attrs)
    chosen = list(footprints)
    plan = [{"key": f"fp{fp.index:05d}", "uuid": fp.uuid, "ref": fp.ref,
             "x_nm": round(fp.x * 1e6), "y_nm": round(fp.y * 1e6),
             "keep_instance_attrs": fp.index in keep} for fp in chosen]
    with tempfile.TemporaryDirectory(prefix="kicad_import_") as tmp:
        result = _run_swig(kicad_python, board_path, plan, Path(tmp), timeout_s)
        saved = [(fp, strip_instance_fields(
                    (result["dir"] / f"{e['key']}.pretty" / f"{e['key']}.kicad_mod")
                    .read_text(encoding="utf-8"))) for fp, e in zip(chosen, plan, strict=True)]
    names, texts, variants = _assign_names(saved)
    files = _install(texts, Path(out_pretty))
    return FootprintLibrary(names={i: f"{nick}:{n}" for i, n in names.items()},
                            files=tuple(files), variants=tuple(variants),
                            warnings=tuple(result.get("warnings", ())),
                            pcbnew=str(result.get("pcbnew", "")),
                            format=written_format(texts.values()))


def dialect_warning(lib: FootprintLibrary) -> str | None:
    """Why the extracted library is not in the D0 dialect, or None."""
    if lib.format == K9_PCB_FORMAT:
        return None
    return (f"pcbnew {lib.pcbnew} wrote the footprints as format {lib.format}, not KiCad 9's "
            f"{K9_PCB_FORMAT} (D0): KiCad 9 cannot load this library. Install KiCad 9 "
            "(choose_swig_install prefers it) and import again for a KiCad 9 rebuild")


def write_footprint_library(pcb: Path, board: BoardDesign, keep_instance_attrs: Iterable[int],
                            nick: str, path: Path, kicad_python: str | None, enabled: bool
                            ) -> tuple[FootprintLibrary | None, dict]:
    """``path`` (.pretty) from every footprint of the board (D4: a source
    board in the D0 dialect gives the rebuild its own footprints), or the
    reason it was not extracted, as (library | None, report entry). Without
    ``kicad_python`` the pcbnew of choose_swig_install is used; the report
    entry records the dialect written (``format``, ``d0_dialect``)."""
    info: dict[str, Any] = {"file": str(path), "extracted": False}
    if not enabled:
        info["reason"] = "footprint extraction was turned off"
        return None, info
    if board.format_version is not None and board.format_version > K9_PCB_FORMAT:
        info["reason"] = (f"the board is format {board.format_version}, newer than KiCad 9's "
                          f"{K9_PCB_FORMAT}: its footprints are not extracted")
        info["kicad11_path_required"] = True
        return None, info
    if kicad_python is None:
        kicad_python, why = _swig_python(board.format_version)
        if kicad_python is None:
            info["reason"] = why
            return None, info
    lib = extract_footprint_library(pcb, board, board.footprints, nick, path, kicad_python,
                                    keep_instance_attrs=keep_instance_attrs)
    warnings = list(lib.warnings)
    if (note := dialect_warning(lib)) is not None:
        warnings.append(note)
    info.update(extracted=True, footprints=len(lib.files), variants=list(lib.variants),
                pcbnew=lib.pcbnew, python=kicad_python, format=lib.format,
                d0_dialect=lib.format == K9_PCB_FORMAT, warnings=warnings)
    return lib, info


def project_footprint_libs(root: Path, board: BoardDesign) -> dict[str, str]:
    """{nick: absolute .pretty} for the board's footprint nicknames that the
    source project's own fp-lib-table defines: the netlist ``libraries``
    block when the footprints are not extracted."""
    import libtable
    table_path = root / "fp-lib-table"
    if not table_path.is_file():
        return {}
    used = {fp.lib_id.partition(":")[0] for fp in board.footprints if ":" in fp.lib_id}
    out: dict[str, str] = {}
    for row in libtable.read_table(table_path).rows:
        if row.name in used and row.is_kicad and not row.disabled:
            uri, missing = libtable.expand_vars(row.uri, kiprjmod=root)
            if not missing and Path(uri).is_dir():
                out[row.name] = str(Path(uri).resolve())
    return out
