#!/usr/bin/env python3
"""Footprint file dialects and the pcbnew that has to load them (ROADMAP D0).

pcbnew.FootprintLoad returns None, without a word, for a footprint written in
a newer dialect than it reads: KiCad 9's pcbnew loads footprints up to
``(version 20241229)``, KiCad 10's up to ``20260206``, and neither loads a
KiCad 11 ``20260901`` one. swig_build_board turns that None into
load_failure_message(): the .kicad_mod, its ``(version N)``, the pcbnew build
(``pcbnew.GetBuildVersion()``) and the newest format that build loads.

Stdlib only and Python 3.11-safe: swig_build_board imports it under KiCad's
bundled Python (9.0.4 ships 3.11.5).
"""
from __future__ import annotations

import os
import re

KICAD9_FOOTPRINT_VERSION = 20241229     # D0: the dialect every authored footprint uses
KICAD10_FOOTPRINT_VERSION = 20260206
# release major -> the newest footprint (version N) that release's pcbnew loads
FOOTPRINT_LIMITS = {9: KICAD9_FOOTPRINT_VERSION, 10: KICAD10_FOOTPRINT_VERSION}
NIGHTLY_MINOR = 99                      # x.99 = the nightly of x+1: its dialect still moves
HEADER_BYTES = 8192                     # the (version N) header is among the first lines
FOOTPRINT_ROOTS = ("footprint", "module")
CONVERT_HINT = ("convert it to KiCad 9's dialect (python server/tools/kicad_dialect.py fp "
                "<src.pretty> <dst.pretty>), author it with fp_author (D0 dialect), or build "
                "with a pcbnew that reads it")

_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()"]+')
_RELEASE = re.compile(r"(\d+)\.(\d+)")
_KNOWN = f"KiCad 9 loads <= {KICAD9_FOOTPRINT_VERSION}, KiCad 10 <= {KICAD10_FOOTPRINT_VERSION}"


def _head(path: str | os.PathLike) -> str | None:
    """The first HEADER_BYTES of a file as text (BOM dropped), None if unreadable."""
    try:
        with open(path, "rb") as f:
            raw = f.read(HEADER_BYTES)
    except OSError:                     # missing, a directory, no permission
        return None
    return raw.decode("utf-8", errors="replace").lstrip("\ufeff")


def footprint_format_version(path: str | os.PathLike) -> int | None:
    """The ``(version N)`` of a .kicad_mod: the root list's own child, never a
    nested one or text inside a quoted string. None for a missing or unreadable
    file, text that is not a footprint, a KiCad 5 ``(module ...)`` without a
    version, or a header that does not fit in HEADER_BYTES."""
    head = _head(path)
    toks = _TOKEN.findall(head) if head else []
    if len(toks) < 2 or toks[0] != "(" or toks[1] not in FOOTPRINT_ROOTS:
        return None
    depth = 0
    for i, tok in enumerate(toks):
        if tok == ")":
            depth -= 1
            if depth <= 0:
                return None
        elif tok == "(":
            depth += 1
            if depth == 2 and toks[i + 1:i + 2] == ["version"] and toks[i + 3:i + 4] == [")"] \
                    and toks[i + 2].isdigit():
                return int(toks[i + 2])
    return None


def pcbnew_footprint_limit(build_version: str | None) -> int | None:
    """The newest footprint ``(version N)`` the pcbnew of ``build_version``
    (``pcbnew.GetBuildVersion()``, e.g. "9.0.4", "(10.0.5-1)") loads, from
    FOOTPRINT_LIMITS. None when unknown: a nightly (x.99, its dialect still
    moves), a release this table does not list, or text without major.minor."""
    found = _RELEASE.search(str(build_version or ""))
    if found is None:
        return None
    major, minor = int(found.group(1)), int(found.group(2))
    return None if minor >= NIGHTLY_MINOR else FOOTPRINT_LIMITS.get(major)


def _missing(path: str) -> str:
    library = os.path.dirname(path)
    if not os.path.isdir(library):
        return f"the footprint library {library} does not exist (looked for {path})"
    name = os.path.splitext(os.path.basename(path))[0]
    return f"{path} does not exist (no footprint '{name}' in {library})"


def _verdict(path: str, version: int | None, build: str, limit: int | None) -> str:
    if version is None:
        found = f"{path} has no (version N) header (not a KiCad 6+ footprint)"
    else:
        found = f"{path} is (version {version})"
    if limit is None:
        return (f"{found}; the newest footprint format pcbnew {build} loads is unknown "
                f"({_KNOWN}); {CONVERT_HINT}")
    if version is not None and version > limit:
        return (f"{found}, newer than (version {limit}), the newest footprint format "
                f"pcbnew {build} loads ({_KNOWN}); {CONVERT_HINT}")
    return (f"{found} and pcbnew {build} loads footprints up to (version {limit}), so the "
            f"file itself is unreadable (malformed, or not a KiCad footprint): check it with "
            f"kicad-cli fp upgrade")


def load_failure_message(path: str | os.PathLike, build_version: str | None,
                         lib_id: str = "") -> str:
    """One line for a footprint pcbnew did not load (FootprintLoad returned
    None or raised): the .kicad_mod ``path``, its ``(version N)``, the pcbnew
    build and the newest format that build loads (FOOTPRINT_LIMITS), with the
    likely cause: a missing file or library, a too-new dialect, or a broken
    file. ``lib_id`` ("nick:name") is named when given. Never raises."""
    path = os.fspath(path)
    build = str(build_version or "").strip() or "unknown"
    what = f"footprint {lib_id}" if lib_id else "a footprint"
    lead = f"pcbnew {build} could not load {what}"
    if not os.path.isfile(path):
        return f"{lead}: {_missing(path)}"
    verdict = _verdict(path, footprint_format_version(path), build, pcbnew_footprint_limit(build))
    return f"{lead}: {verdict}"
