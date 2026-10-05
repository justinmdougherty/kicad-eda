#!/usr/bin/env python3
"""Load a foreign board with KiCad's own importer through pcbnew SWIG and save
it as a .kicad_pcb (ROADMAP_DECISIONS D4: the board fallback when no
kicad-cli has `pcb import`). Runs under KiCad's bundled Python, so it uses the
stdlib and pcbnew only and stays Python 3.11-compatible (KiCad 9.0.4).

Importers, by the major version of the running pcbnew (pcbnew.Version()):
  ALTIUM_DESIGNER  Altium .PcbDoc      9.x and 10.x
  EASYEDA          EasyEDA Std .json   9.x and 10.x
  EAGLE            Eagle .brd          10.x only. On 9.0.4 LoadBoard(EAGLE)
                   returns None, so it is refused BEFORE anything loads, with
                   code needs_newer_kicad.
The plugin crosses the process boundary as a NAME and is resolved here
(getattr(pcbnew.PCB_IO_MGR, name)): the enum values differ between releases
(10.0 inserted ALLEGRO=3). Never ``PCB_IO_MGR.Load`` + ``SaveBoard`` (9.0.4
segfaults, 10.0.5 aborts) and never LoadBoard without a type (None on both).
EasyEDA Pro is not offered: LoadBoard(EASYEDAPRO) returns None on 9.0.4.

LoadBoard always reads a COPY of the input placed in a temp dir: importers
write side files beside their input (the Eagle importer writes
<stem>.kicad_dru, and on a read-only source dir logs "can't open file ...
(error 30)"). Their names are reported as importer_side_files; the temp dir
and everything in it is discarded.

Diagnostics on the loaded board (zones = copper zones, never rule areas):
  zones_on_disabled_layers  {layer: count} for zones on copper layers beyond
      GetCopperLayerCount() (EasyEDA Std boards load as 2-layer boards with
      zones on In1.Cu / In2.Cu); {} when there are none.
  inner_plane_loss  ALTIUM_DESIGNER on pcbnew < 10 only: 9.0.4 moves Altium
      Internal Plane layers to User.Eco1 as netless zones. null when nothing
      was lost, else {"zones": zones on Eco1.User, "nets": the Altium plane
      nets (Board6 PLANEnNETNAME and split-plane polygons of the stacked
      planes, read from the .PcbDoc by the stdlib OLE reader below) that have
      no zone on any In*.Cu}. An object with "error" means a loss could not be
      ruled out: treat it as a loss.
  diagnostics_errors  [str], present only when a check raised (that check's
      field is then null, or an inner_plane_loss object with "error").

The caller runs this with stdin=DEVNULL; kicad_quiet (imported before pcbnew)
turns wx asserts off (the Altium importer asserts "Unexpected Layer ID") and
sends wx log messages to stderr, so no modal dialog can block the child.

Usage: <kicad-python> swig_load_foreign.py <input> <output.kicad_pcb> <PLUGIN>
       PLUGIN is ALTIUM_DESIGNER, EASYEDA or EAGLE.
Output: the LAST stdout line is JSON
  {"ok": bool, "plugin", "input", "output", "pcbnew",
   "footprints", "tracks", "vias", "nets", "zones", "copper_layers",
   "importer_side_files", "zones_on_disabled_layers", "inner_plane_loss",
   ["diagnostics_errors"], ["code", "error"]}
  code (failures only): usage | unknown_plugin | needs_newer_kicad |
  input_not_found | copy_failed | load_raised | load_returned_none | save_failed
Exit codes: 0 saved, 1 load/save failed, 2 usage or refused plugin.
"""
from __future__ import annotations

import faulthandler
import json
import os
import re
import shutil
import struct
import sys
import tempfile
from collections import Counter
from typing import NamedTuple

import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew

PLUGINS = ("ALTIUM_DESIGNER", "EASYEDA", "EAGLE")
MIN_MAJOR = {"EAGLE": 10}
NEEDS_NEWER = {"EAGLE": "LoadBoard(EAGLE) returns None on KiCad 9: use kicad-cli pcb import "
                        "(10.0+) or the 10.99 import"}
OK_EXIT, FAIL_EXIT, USAGE_EXIT = 0, 1, 2
_VERSION_RE = re.compile(r"(\d+)\.(\d+)")


class Refused(ValueError):
    """A plugin this pcbnew must not run; ``code`` says why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def emit(ok: bool, exit_code: int, **fields: object) -> int:
    """Print the result line (always the last stdout line) and return `exit_code`."""
    print(json.dumps({"ok": ok, **fields}), flush=True)
    return exit_code


def pcbnew_major(version: str) -> int:
    """'9.0.4' -> 9, '(10.0.5-1)' -> 10; 0 when no version can be read."""
    found = _VERSION_RE.search(version or "")
    return int(found.group(1)) if found else 0


def plugin_type(name: str, major: int) -> int:
    """PCB_IO_MGR.<name> for a plugin this pcbnew major version may run
    (Refused otherwise)."""
    if name not in PLUGINS:
        raise Refused("unknown_plugin", f"plugin {name!r} is not one of {', '.join(PLUGINS)}")
    if major < MIN_MAJOR.get(name, 0):
        raise Refused("needs_newer_kicad",
                      f"plugin {name} refused (pcbnew major version {major}, needs "
                      f"{MIN_MAJOR[name]}+): {NEEDS_NEWER[name]}")
    return getattr(pcbnew.PCB_IO_MGR, name)


def board_counts(board: object) -> dict:
    """What the loaded board holds (track/arc segments and vias apart)."""
    tracks = list(board.GetTracks())
    vias = sum(1 for t in tracks if t.GetClass() == "PCB_VIA")
    return {"footprints": len(board.GetFootprints()), "tracks": len(tracks) - vias,
            "vias": vias, "nets": board.GetNetCount(), "zones": board.GetAreaCount(),
            "copper_layers": board.GetCopperLayerCount()}


# ---------------------------------------------------------------------------
# A read-only OLE2 compound file reader (MS-CFB: the Altium .PcbDoc container)
# ---------------------------------------------------------------------------
CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_MARKER = 0xFFFFFFFA            # sector ids from here up: end of chain, free, FAT, DIFAT
_STORAGE, _STREAM, _ROOT = 1, 2, 5


class _Entry(NamedTuple):
    name: str                   # upper case: compound-file names compare case-blind
    kind: int
    left: int
    right: int
    child: int
    start: int
    size: int


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _u32s(data: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(data) // 4}I", data[:len(data) // 4 * 4]))


class CompoundFile:
    """The streams of an OLE2 compound file, by "Storage/Stream" path."""

    def __init__(self, data: bytes) -> None:
        if len(data) < 512 or data[:8] != CFB_MAGIC:
            raise ValueError("not an OLE compound file")
        self._data = data
        self._size, self._mini_size = 1 << _u16(data, 30), 1 << _u16(data, 32)
        if self._size not in (512, 4096) or self._mini_size != 64:
            raise ValueError("unsupported compound-file sector size")
        self._cutoff = _u32(data, 56)
        self._fat = self._read_fat()
        self._entries = self._read_directory()
        root = self._entries[0]
        self._mini = self._chain_bytes(root.start)[:root.size]
        self._minifat = _u32s(self._chain_bytes(_u32(data, 60)))

    def _sector(self, sid: int) -> bytes:
        start = (sid + 1) * self._size
        if start >= len(self._data):
            raise ValueError(f"sector {sid} lies beyond the end of the file")
        return self._data[start:start + self._size]

    def _chain(self, start: int, table: list[int]) -> list[int]:
        sids: list[int] = []
        sid = start
        while sid < _MARKER:
            if sid >= len(table) or len(sids) > len(table):
                raise ValueError("broken or cyclic sector chain")
            sids.append(sid)
            sid = table[sid]
        return sids

    def _chain_bytes(self, start: int) -> bytes:
        return b"".join(self._sector(s) for s in self._chain(start, self._fat))

    def _read_fat(self) -> list[int]:
        data = self._data
        sids = list(struct.unpack_from("<109I", data, 76))
        sid = _u32(data, 68)
        for _ in range(min(_u32(data, 72), len(data) // self._size)):   # DIFAT sectors
            if sid >= _MARKER:
                break
            block = _u32s(self._sector(sid))
            sids, sid = sids + block[:-1], block[-1]
        fat_sids = [s for s in sids if s < _MARKER][:_u32(data, 44)]
        return [entry for s in fat_sids for entry in _u32s(self._sector(s))]

    def _read_directory(self) -> list[_Entry]:
        raw = self._chain_bytes(_u32(self._data, 48))
        entries = []
        for off in range(0, len(raw) - 127, 128):
            name_len = min(_u16(raw, off + 64), 64)
            name = raw[off:off + max(name_len - 2, 0)].decode("utf-16-le", "replace")
            left, right, child = struct.unpack_from("<3I", raw, off + 68)
            start, size = struct.unpack_from("<IQ", raw, off + 116)
            size = size & 0xFFFFFFFF if self._size == 512 else size
            entries.append(_Entry(name.upper(), raw[off + 66], left, right, child, start, size))
        if not entries or entries[0].kind != _ROOT:
            raise ValueError("compound file without a root directory entry")
        return entries

    def _children(self, index: int) -> dict[str, int]:
        """{NAME: entry index} of a storage's children (its sibling tree)."""
        kids: dict[str, int] = {}
        todo, seen = [self._entries[index].child], set()
        while todo:
            i = todo.pop()
            if i >= len(self._entries) or i in seen:
                continue
            seen.add(i)
            entry = self._entries[i]
            kids.setdefault(entry.name, i)
            todo += [entry.left, entry.right]
        return kids

    def _lookup(self, path: str) -> _Entry | None:
        index = 0
        for part in path.split("/"):
            kids = self._children(index)
            if part.upper() not in kids:
                return None
            index = kids[part.upper()]
        return self._entries[index]

    def has(self, path: str) -> bool:
        entry = self._lookup(path)
        return entry is not None and entry.kind == _STREAM

    def stream(self, path: str) -> bytes:
        """The bytes of stream `path` ("Storage/Stream", case-blind)."""
        entry = self._lookup(path)
        if entry is None:
            raise KeyError(f"no {path} in the compound file")
        if entry.kind != _STREAM:
            raise ValueError(f"{path} is not a stream")
        if entry.size < self._cutoff:
            m = self._mini_size
            data = b"".join(self._mini[s * m:(s + 1) * m]
                            for s in self._chain(entry.start, self._minifat))
        else:
            data = b"".join(self._sector(s) for s in self._chain(entry.start, self._fat))
        if len(data) < entry.size:
            raise ValueError(f"{path} is truncated ({len(data)} of {entry.size} bytes)")
        return data[:entry.size]


# ---------------------------------------------------------------------------
# Altium plane nets (Board6 / Polygons6 / Nets6 property records)
# ---------------------------------------------------------------------------
NO_NET = {"", "(NO NET)", "(MULTIPLE NETS)"}
_TOP, _BOTTOM, _PLANE1, _PLANES = 1, 32, 39, 16   # Altium V6 layer ids: PLANE k = 38 + k


def _properties(body: bytes) -> dict[str, str]:
    """|KEY=VALUE|... -> {KEY: VALUE}; a %UTF8%KEY twin wins over KEY."""
    props: dict[str, str] = {}
    for item in body.rstrip(b"\0").split(b"|"):
        key, sep, value = item.partition(b"=")
        name = key.decode("latin-1").strip().upper()
        if not sep or not name:
            continue
        if name.startswith("%UTF8%"):
            props[name[len("%UTF8%"):]] = value.decode("utf-8", "replace")
        else:
            props.setdefault(name, value.decode("latin-1"))
    return props


def altium_records(stream: bytes) -> list[dict[str, str]]:
    """The property records of an Altium <Name>6/Data stream: each one is
    <u32 length><|KEY=VALUE|...\\0>; a length with its top byte set marks a
    binary record, which is skipped."""
    records, pos = [], 0
    while pos + 4 <= len(stream):
        head = _u32(stream, pos)
        length, pos = head & 0x00FFFFFF, pos + 4
        if pos + length > len(stream):
            raise ValueError(f"truncated Altium record at byte {pos - 4}")
        if not head & 0xFF000000:
            records.append(_properties(stream[pos:pos + length]))
        pos += length
    return records


def _int(text: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        return -1


def _stacked_planes(board: dict[str, str]) -> set[int] | None:
    """Plane numbers in the layer stack (the LAYERnNEXT walk from TOP to
    BOTTOM); None when the record has no complete stack (every plane counts)."""
    nxt = {}
    for key, value in board.items():
        found = re.fullmatch(r"LAYER(\d+)NEXT", key)
        if found:
            nxt[int(found.group(1))] = _int(value)
    walk, layer = [], _TOP
    while layer in nxt and layer not in walk:
        walk, layer = walk + [layer], nxt[layer]
    if _BOTTOM not in walk + [layer]:
        return None
    return {lid - _PLANE1 + 1 for lid in walk if _PLANE1 <= lid < _PLANE1 + _PLANES}


def _plane_number(text: str) -> int:
    found = re.fullmatch(r"PLANE(\d+)", text.strip().upper())
    return int(found.group(1)) if found else -1


def altium_plane_nets(data: bytes) -> set[str]:
    """Net names on the stacked internal planes of an Altium .PcbDoc: each
    plane's PLANEnNETNAME plus the nets of its split-plane polygons."""
    cfb = CompoundFile(data)
    boards = altium_records(cfb.stream("Board6/Data"))
    board = boards[0] if boards else {}
    names = [r.get("NAME", "") for r in altium_records(cfb.stream("Nets6/Data"))]
    stacked = _stacked_planes(board)
    nets = set()
    for key, value in board.items():
        found = re.fullmatch(r"PLANE(\d+)NETNAME", key)
        if found and (stacked is None or int(found.group(1)) in stacked) and \
                value.strip().upper() not in NO_NET:
            nets.add(value.strip())
    polygons = altium_records(cfb.stream("Polygons6/Data")) \
        if cfb.has("Polygons6/Data") else []
    for poly in polygons:
        plane, index = _plane_number(poly.get("LAYER", "")), _int(poly.get("NET", ""))
        if plane > 0 and (stacked is None or plane in stacked) and \
                poly.get("POLYGONTYPE", "").strip().upper() == "SPLIT PLANE" and \
                0 <= index < len(names) and names[index]:
            nets.add(names[index])
    return nets


# ---------------------------------------------------------------------------
# Diagnostics on the loaded board
# ---------------------------------------------------------------------------
def _copper_zones(board: object) -> list:
    return [z for z in board.Zones() if not z.GetIsRuleArea()]


def _layers(zone: object) -> tuple:
    return tuple(zone.GetLayerSet().Seq())


def _is_inner_copper(layer: int) -> bool:
    return bool(pcbnew.IsCopperLayer(layer)) and layer not in (pcbnew.F_Cu, pcbnew.B_Cu)


def zones_on_disabled_layers(board: object) -> dict[str, int]:
    """{layer: zone count} for zones on copper layers the board does not
    have (beyond GetCopperLayerCount())."""
    count = board.GetCopperLayerCount()
    enabled = {pcbnew.F_Cu, pcbnew.B_Cu} | \
        {getattr(pcbnew, f"In{k}_Cu") for k in range(1, count - 1)}
    hits = Counter(pcbnew.BOARD.GetStandardLayerName(layer)
                   for zone in _copper_zones(board) for layer in _layers(zone)
                   if pcbnew.IsCopperLayer(layer) and layer not in enabled)
    return dict(sorted(hits.items()))


def inner_plane_loss(board: object, source: str) -> dict | None:
    """Altium internal planes lost by a pcbnew 9 import (see the docstring)."""
    zones = _copper_zones(board)
    moved = sum(1 for z in zones if pcbnew.Eco1_User in _layers(z))
    on_inner = {z.GetNetname() for z in zones if any(map(_is_inner_copper, _layers(z)))}
    try:
        with open(source, "rb") as f:
            planes = altium_plane_nets(f.read())
    except (OSError, KeyError, ValueError, struct.error) as e:
        return {"zones": moved, "nets": None,
                "error": f"Altium plane nets unreadable: {type(e).__name__}: {e}"}
    lost = sorted(planes - on_inner)
    return {"zones": moved, "nets": lost} if moved or lost else None


def diagnostics(board: object, plugin: str, major: int, source: str) -> dict:
    """zones_on_disabled_layers + inner_plane_loss (+ diagnostics_errors); a
    check that raises is reported, never allowed to sink a good load."""
    errors: list[str] = []
    try:
        disabled = zones_on_disabled_layers(board)
    except Exception as e:  # an unexpected pcbnew API: report, keep the board
        disabled = None
        errors.append(f"zones_on_disabled_layers: {type(e).__name__}: {e}")
    loss = None
    if plugin == "ALTIUM_DESIGNER" and major < 10:
        try:
            loss = inner_plane_loss(board, source)
        except Exception as e:  # fail closed: a loss that cannot be ruled out
            why = f"{type(e).__name__}: {e}"
            loss = {"zones": None, "nets": None, "error": why}
            errors.append(f"inner_plane_loss: {why}")
    result = {"zones_on_disabled_layers": disabled, "inner_plane_loss": loss}
    return {**result, "diagnostics_errors": errors} if errors else result


# ---------------------------------------------------------------------------
# Load (from a temp copy) and save
# ---------------------------------------------------------------------------
def save(board: object, out: str, base: dict, facts: dict) -> int:
    try:
        saved = pcbnew.SaveBoard(out, board)
    except Exception as e:
        return emit(False, FAIL_EXIT, **base, **facts, code="save_failed",
                    error=f"SaveBoard raised {type(e).__name__}: {e}")
    if saved is False or not os.path.isfile(out):
        return emit(False, FAIL_EXIT, **base, **facts, code="save_failed",
                    error=f"SaveBoard did not write {out}")
    return emit(True, OK_EXIT, **base, **facts)


def load_and_save(src: str, out: str, kind: int, major: int, base: dict) -> int:
    """LoadBoard a copy of `src` in a temp dir, diagnose it, SaveBoard to `out`."""
    name = base["plugin"]
    with tempfile.TemporaryDirectory(prefix="kicad-eda-foreign-",
                                     ignore_cleanup_errors=True) as tmp:
        copy = os.path.join(tmp, os.path.basename(src))
        try:
            shutil.copyfile(src, copy)
        except OSError as e:
            return emit(False, FAIL_EXIT, **base, code="copy_failed",
                        error=f"cannot copy the input into a temp dir: {e}")
        try:
            board = pcbnew.LoadBoard(copy, kind)
        except Exception as e:  # pcbnew raises IOError (and friends) for unreadable files
            return emit(False, FAIL_EXIT, **base, code="load_raised",
                        error=f"LoadBoard({name}) raised {type(e).__name__}: {e}")
        if board is None:
            return emit(False, FAIL_EXIT, **base, code="load_returned_none",
                        error=f"LoadBoard({name}) returned None: not a {name} board, or "
                              f"this pcbnew cannot import it")
        side_files = sorted(set(os.listdir(tmp)) - {os.path.basename(copy)})
        facts = {**board_counts(board), "importer_side_files": side_files,
                 **diagnostics(board, name, major, copy)}
        return save(board, out, base, facts)


def main(argv: list[str]) -> int:
    faulthandler.enable()        # a native crash still leaves a traceback on stderr
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return emit(False, USAGE_EXIT, code="usage",
                    error="usage: swig_load_foreign.py <input> <output.kicad_pcb> <PLUGIN>")
    src, out, name = os.path.abspath(argv[1]), os.path.abspath(argv[2]), argv[3]
    version = pcbnew.Version()
    major = pcbnew_major(version)
    base = {"plugin": name, "input": src, "output": out, "pcbnew": version}
    try:
        kind = plugin_type(name, major)
    except Refused as e:
        return emit(False, USAGE_EXIT, **base, code=e.code, error=str(e))
    if not os.path.isfile(src):
        return emit(False, FAIL_EXIT, **base, code="input_not_found",
                    error=f"input not found: {src}")
    if not out.endswith(".kicad_pcb"):
        return emit(False, USAGE_EXIT, **base, code="usage",
                    error=f"output must be a .kicad_pcb: {out}")
    return load_and_save(src, out, kind, major, base)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
