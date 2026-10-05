#!/usr/bin/env python3
"""One resolver for the netlist ``board`` block (ROADMAP_DECISIONS D2).

Lint, the board builder, the mechanical tools and G5 must agree on ONE
outline, and swig_build_board runs under KiCad's Python, which cannot import
ezdxf. This module (venv side) turns any board block into a normalised one:

  source         the board block                     is_legacy
  "none"         absent / null / {}                  yes (auto-sized board)
  "rect"         w/h (corner_radius 0)               yes, unless cutouts / datum
  "rounded_rect" w/h + corner_radius > 0             no
  "loop"         outline.loop (+ cutouts, datum)     no
  "dxf"          outline.dxf (+ layer, units)        no

Everything else raises ResolveError, as netlist_schema refuses it: a
non-empty block needs w/h or an outline (auto_* windows exist only inside a
fixed board), an outline has exactly one of loop / dxf and no unknown key,
and layer / units apply only to a dxf (a loop is always mm).

A ``{"dxf": ...}`` outline is replaced by the loop, cutouts and datum that
dxf_io.outline_from_spec reads (path relative to the netlist directory, or
``${KIPRJMOD}`` / absolute); every other key (auto_*, ...) is kept. The DXF
defines the board, so w/h must match its bbox (1e-3 mm), and corner_radius,
cutouts and datum next to it are refused (draw them in the DXF). Legacy
boards keep the builder's current path bit for bit: their block is a plain
copy. Imports are stdlib + outline.py (Python 3.11, so the builder's KiCad
Python can import it too); dxf_io / ezdxf are imported only for a DXF.

Fingerprint: sha256 over the canonical JSON of the resolved block
(``sort_keys``, separators ``(",", ":")``), then, for a DXF, ``b"\\0dxf\\0"``
and the DXF file bytes, so a re-exported DXF invalidates the route cache.

Sidecar (version 1). THE CONTRACT swig_build_board reads through the
environment variable KICAD_EDA_OUTLINE (F3-11): build_board_from_netlist
writes it next to the board as ``<pcb stem>.outline.json`` (sidecar_path)
and the builder uses its ``board`` in place of the netlist's. UTF-8 JSON,
written atomically (temp file + os.replace), keys in this order::

    {"version": 1,
     "board": {"w": 60.0, "h": 40.0, "outline": {"loop": [[x, y], [x, y, bulge]]},
               "cutouts": [{"loop": [...]}, {"circle": [cx, cy, r]}],
               "datum": [10.0, 10.0], "auto_x": 5.0},
     "source": "none" | "rect" | "rounded_rect" | "loop" | "dxf",
     "dxf": "<absolute path of the DXF read>" | null,
     "fingerprint": "<64 lowercase hex digits>",
     "warnings": ["<DXF reader warning>", ...]}

``board`` is the resolved block (never a dxf reference; mm, board frame,
Y down, bbox top-left = 0, 0); ``dxf`` is set exactly when source is "dxf".
read_sidecar rejects any other version, a missing or unknown key, or a
wrongly typed value.

Usage: <python> outline_resolve.py <netlist.json> [--sidecar out.json]
       -> {"ok": true, "source", "fingerprint", "bbox", "size", "is_legacy",
           "warnings", "sidecar"}, exit 0  |  {"ok": false, "error"}, exit 1
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import outline as ol  # noqa: E402

__all__ = ["ENV_VAR", "ResolveError", "ResolvedBoard", "SIDECAR_KEYS", "SIDECAR_VERSION",
           "SOURCES", "main", "outline_bbox_dims", "read_sidecar", "resolve_board",
           "resolved_design", "sidecar_path", "write_sidecar"]

SOURCES = ("none", "rect", "rounded_rect", "loop", "dxf")
SIDECAR_VERSION = 1
SIDECAR_KEYS = ("version", "board", "source", "dxf", "fingerprint", "warnings")
SIDECAR_SUFFIX = ".outline.json"
ENV_VAR = "KICAD_EDA_OUTLINE"
_DXF_ONLY = ("layer", "units")
_SPEC_KEYS = ("dxf", "layer", "loop", "units")              # netlist_schema $defs.outline
_DXF_REPLACES = ("w", "h", "outline", "cutouts", "datum")   # the DXF supplies these
_DXF_SEPARATOR = b"\x00dxf\x00"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ResolveError(ValueError):
    """The board block cannot be resolved; the message is the underlying
    OutlineError / DxfOutlineError text, or names the offending key."""


@dataclass(frozen=True)
class ResolvedBoard:
    """A resolved board block. ``block`` is read-only by convention (copy it,
    e.g. through resolved_design, before changing anything)."""
    source: str
    block: dict
    outline: ol.Outline | None
    dxf_path: str | None
    fingerprint: str
    warnings: tuple[str, ...] = ()

    @property
    def is_legacy(self) -> bool:
        """True when the builder keeps today's path: no board geometry, or a
        plain w x h rectangle without cutouts or datum."""
        if self.source == "none":
            return True
        return (self.source == "rect" and not self.block.get("cutouts")
                and self.block.get("datum") is None)


# --- resolving ------------------------------------------------------------------------
def _fingerprint(block: Mapping[str, Any], dxf_bytes: bytes | None) -> str:
    try:
        text = json.dumps(block, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as e:
        raise ResolveError(f"the board block is not plain JSON: {e}") from e
    digest = hashlib.sha256(text.encode("utf-8"))
    if dxf_bytes is not None:
        digest.update(_DXF_SEPARATOR)
        digest.update(dxf_bytes)
    return digest.hexdigest()


def _validated(block: Mapping[str, Any]) -> ol.Outline:
    try:
        return ol.from_board_block(block)
    except (TypeError, ValueError) as e:            # OutlineError is a ValueError
        raise ResolveError(str(e)) from e


def _none() -> ResolvedBoard:
    return ResolvedBoard("none", {}, None, None, _fingerprint({}, None), ())


def _check_outline_spec(spec: Mapping[str, Any]) -> None:
    """netlist_schema's outline rules, so no geometry is silently dropped
    (a loop beside a dxf) or misread (a loop 'in cm')."""
    unknown = sorted(str(k) for k in spec if k not in _SPEC_KEYS)
    if unknown:
        raise ResolveError(f"board.outline has unknown key(s) {', '.join(map(repr, unknown))} "
                           f"(allowed: {', '.join(_SPEC_KEYS)})")
    if "loop" not in spec:
        return
    if "dxf" in spec:
        raise ResolveError("board.outline has both 'loop' and 'dxf', but an outline has "
                           "exactly one of 'loop' or 'dxf': drop the loop to use the DXF, or "
                           "drop the dxf to keep the loop")
    if any(k in spec for k in _DXF_ONLY):
        raise ResolveError("board.outline: 'layer' and 'units' only apply to a 'dxf' outline; "
                           "a loop is always mm in the board frame")


def _finite(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value):
        raise ResolveError(f"{what} must be a finite number, got {value!r}")
    return float(value)


def _check_dxf_neighbours(board: Mapping[str, Any], spec: Mapping[str, Any],
                          base_dir: str | Path | None) -> None:
    if base_dir is None:
        raise ResolveError(f"board.outline.dxf {spec.get('dxf')!r} needs base_dir (the "
                           "netlist directory) to be resolved: call resolve_board(design, "
                           "Path(netlist).parent)")
    if board.get("cutouts"):
        raise ResolveError("board.cutouts cannot be combined with a dxf outline: the DXF's "
                           "inner loops are the cutouts, so draw them in the DXF (or embed the "
                           "outline as outline.loop and list the cutouts there)")
    if board.get("datum") is not None:
        raise ResolveError("board.datum cannot be combined with a dxf outline: the datum is "
                           "the DXF origin; remove board.datum")


def _check_dxf_size(board: Mapping[str, Any], geom: Mapping[str, Any]) -> None:
    for key in ("w", "h"):
        if board.get(key) is None:
            continue
        have = _finite(board[key], f"board.{key}")
        if abs(have - geom[key]) > ol.FRAME_TOL_MM:
            raise ResolveError(f"board.{key} = {board[key]!r} contradicts the DXF outline bbox "
                               f"({geom[key]:.4f} mm); drop it (the DXF defines the board) or "
                               "fix the DXF")


def _resolve_dxf(board: Mapping[str, Any], spec: Mapping[str, Any],
                 base_dir: str | Path | None) -> ResolvedBoard:
    _check_dxf_neighbours(board, spec, base_dir)
    try:
        import dxf_io
    except ImportError as e:
        raise ResolveError("board.outline.dxf needs the ezdxf package (server/requirements.txt), "
                           f"which is not importable here: {e}") from e
    try:
        read = dxf_io.outline_from_spec(spec, base_dir)
        dxf_path = os.path.abspath(read.source.path)
        data = Path(dxf_path).read_bytes()
    except (dxf_io.DxfOutlineError, OSError) as e:
        raise ResolveError(str(e)) from e
    geom = read.to_board_block()
    _check_dxf_size(board, geom)
    rest = {k: copy.deepcopy(v) for k, v in board.items() if k not in _DXF_REPLACES}
    block = {**geom, **rest}
    outline = _validated(block)
    return ResolvedBoard("dxf", block, outline, dxf_path, _fingerprint(block, data),
                         tuple(read.warnings))


def resolve_board(design: Mapping[str, Any], base_dir: str | Path | None) -> ResolvedBoard:
    """The resolved ``board`` block of `design` (see the module doc).
    `base_dir` is the netlist directory; a dxf outline needs it. Raises
    ResolveError. Never mutates `design`."""
    if not isinstance(design, Mapping):
        raise ResolveError(f"the design must be a JSON object, got {type(design).__name__}")
    board = design.get("board")
    if board is None:
        return _none()
    if not isinstance(board, Mapping):
        raise ResolveError(f"the board block must be an object, got {board!r}")
    if not board:
        return _none()
    spec = board.get("outline")
    if isinstance(spec, Mapping):
        _check_outline_spec(spec)
        if "dxf" in spec:
            return _resolve_dxf(board, spec, base_dir)
    if spec is not None:
        source = "loop"
    else:
        source = "rounded_rect" if board.get("corner_radius") else "rect"
    block = copy.deepcopy(dict(board))
    outline = _validated(block)
    return ResolvedBoard(source, block, outline, None, _fingerprint(block, None), ())


def resolved_design(design: Mapping[str, Any], resolved: ResolvedBoard) -> dict:
    """A NEW design (deep copy) whose ``board`` is ``resolved.block``. For
    source "none" the board key is left as it was (absent stays absent)."""
    out = copy.deepcopy(dict(design))
    if resolved.source != "none":
        out["board"] = copy.deepcopy(resolved.block)
    return out


def outline_bbox_dims(resolved: ResolvedBoard) -> tuple[float, float]:
    """(w, h) in mm: the block's w/h when it has them (checked against the
    outline bbox), else the outline bbox; (0.0, 0.0) for source "none" (the
    builder sizes the board, the same 'not given' as a 0 board_w/board_h)."""
    if resolved.outline is None:
        return (0.0, 0.0)
    w, h = resolved.block.get("w"), resolved.block.get("h")
    if w is not None and h is not None:
        return (float(w), float(h))
    x0, y0, x1, y1 = ol.bbox(resolved.outline)
    return (x1 - x0, y1 - y0)


# --- sidecar --------------------------------------------------------------------------
def sidecar_path(pcb: str | Path) -> str:
    """'<pcb without suffix>.outline.json' (board.kicad_pcb -> board.outline.json)."""
    return str(Path(pcb).with_suffix("")) + SIDECAR_SUFFIX


def _payload(resolved: ResolvedBoard) -> dict[str, Any]:
    return {"version": SIDECAR_VERSION, "board": resolved.block, "source": resolved.source,
            "dxf": resolved.dxf_path, "fingerprint": resolved.fingerprint,
            "warnings": list(resolved.warnings)}


def write_sidecar(resolved: ResolvedBoard, path: str | Path) -> Path:
    """Write the version-1 sidecar (module doc) atomically: a temp file in the
    same directory, then os.replace. A failure leaves any old file intact
    and removes the temp file; OSError propagates. Returns the path."""
    out = Path(path)
    text = json.dumps(_payload(resolved), indent=2) + "\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=out.name + ".", suffix=".tmp", dir=out.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return out


def _board_problems(board: object) -> list[str]:
    if not isinstance(board, dict):
        return [f"'board' must be an object, got {type(board).__name__}"]
    spec = board.get("outline")
    if isinstance(spec, dict) and "dxf" in spec:
        return ["'board.outline' is a dxf reference; a sidecar holds the resolved loops"]
    return []


def _sidecar_problems(data: object) -> list[str]:
    if not isinstance(data, dict):
        return [f"the sidecar must be a JSON object, got {type(data).__name__}"]
    missing = [k for k in SIDECAR_KEYS if k not in data]
    unknown = sorted(set(data) - set(SIDECAR_KEYS))
    if missing or unknown:
        return ([f"missing key(s) {', '.join(missing)}"] if missing else []) + \
               ([f"unknown key(s) {', '.join(unknown)}"] if unknown else [])
    version, source, dxf = data["version"], data["source"], data["dxf"]
    problems = _board_problems(data["board"])
    if isinstance(version, bool) or version != SIDECAR_VERSION:
        problems.append(f"version {version!r} is not {SIDECAR_VERSION} (the only version "
                        "this reader understands)")
    if source not in SOURCES:
        problems.append(f"source {source!r} is not one of {', '.join(SOURCES)}")
    if (source == "dxf") != (isinstance(dxf, str) and bool(dxf)):
        problems.append(f"'dxf' must be the DXF path exactly when source is 'dxf' "
                        f"(source {source!r}, dxf {dxf!r})")
    if not isinstance(data["fingerprint"], str) or not _HEX64.match(data["fingerprint"]):
        problems.append(f"fingerprint {data['fingerprint']!r} is not 64 lowercase hex digits")
    warnings = data["warnings"]
    if not isinstance(warnings, list) or not all(isinstance(w, str) for w in warnings):
        problems.append(f"'warnings' must be a list of strings, got {warnings!r}")
    return problems


def read_sidecar(path: str | Path) -> dict[str, Any]:
    """The sidecar at `path` as a dict, after checking its version, keys and
    value types (module doc). Raises ResolveError."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        raise ResolveError(f"cannot read the outline sidecar {path}: {e}") from e
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ResolveError(f"the outline sidecar {path} is not JSON: {e}") from e
    problems = _sidecar_problems(data)
    if problems:
        raise ResolveError(f"bad outline sidecar {path}: " + "; ".join(problems))
    return data


# --- CLI ------------------------------------------------------------------------------
def _round(values: tuple[float, ...]) -> list[float]:
    return [round(v, 6) + 0.0 for v in values]


def _cli(netlist: str, sidecar: str | None) -> dict[str, Any]:
    path = Path(netlist)
    try:
        design = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as e:
        raise ResolveError(f"cannot read the netlist {path}: {e}") from e
    resolved = resolve_board(design, path.absolute().parent)
    written = str(write_sidecar(resolved, sidecar)) if sidecar else None
    box = ol.bbox(resolved.outline) if resolved.outline is not None else None
    return {"ok": True, "source": resolved.source, "fingerprint": resolved.fingerprint,
            "bbox": _round(box) if box else None,
            "size": _round(outline_bbox_dims(resolved)), "is_legacy": resolved.is_legacy,
            "warnings": list(resolved.warnings), "sidecar": written}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="outline_resolve.py",
                                     description="Resolve a netlist's board block (D2).")
    parser.add_argument("netlist", help="netlist.json (a DXF path resolves against its folder)")
    parser.add_argument("--sidecar", help="also write the KICAD_EDA_OUTLINE sidecar here")
    args = parser.parse_args(argv)
    try:
        result = _cli(args.netlist, args.sidecar)
    except (ResolveError, OSError) as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
