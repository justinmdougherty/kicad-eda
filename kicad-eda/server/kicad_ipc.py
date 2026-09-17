"""KiCad GUI connection over the IPC API (vendored kipy) + the open-document guard.

`connect()` talks to a RUNNING KiCad GUI through its default API socket (the
headless `kicad-cli api-server` a SchematicApi session launches listens on its
own private socket, so it is never mistaken for a GUI here). Every tool that
WRITES a .kicad_sch / .kicad_pcb asks `write_guard()` first: a file that is
open in an editor must not be clobbered from outside. The one exception is a
schematic open in a KiCad 11 GUI, which the API backend edits IN that GUI
(mode "gui"); `gui_can_edit_schematic()` tells the schematic tools when that
route exists. When the API is unreachable the guard cannot know and never
blocks.
"""

from __future__ import annotations

import os
from pathlib import Path

import kicad_env as env

env.use_vendored_kipy()

KICAD11_NIGHTLY = (10, 99)      # a KiCad 11 nightly reports itself as 10.99.x
ENABLE_API_HINT = ("Open KiCad with the API server enabled (Preferences > Plugins and "
                   "Content Manager > Enable KiCad API; KiCad 9: Preferences > API).")
EDITOR_NAMES = {"pcb": "Pcbnew (PCB editor)", "schematic": "Eeschema (schematic editor)",
                "project": "project manager"}
# (kind, DocumentType enum name) — queried in this order; a KiCad without a
# schematic API answers the schematic query with an ApiError, which is tolerated.
DOC_QUERIES = (("pcb", "DOCTYPE_PCB"), ("schematic", "DOCTYPE_SCHEMATIC"),
               ("project", "DOCTYPE_PROJECT"))


def capabilities(ver: tuple[int, ...]) -> dict[str, bool]:
    """Version-derived IPC capabilities: KiCad 11+ (or a 10.99 nightly) gains the
    schematic API, footprint placement from a library and netlist import."""
    is11 = bool(ver) and (ver[0] >= 11 or tuple(ver[:2]) == KICAD11_NIGHTLY)
    return {"schematic_api": is11,
            "place_footprint_from_library": is11,
            "import_netlist": is11}


def kicad_version_of(kicad) -> tuple[str, tuple[int, ...]]:
    """(full version string, numeric tuple) from a connected KiCad."""
    v = kicad.get_version()
    full = str(getattr(v, "full_version", "") or v)
    parts = tuple(getattr(v, a, None) for a in ("major", "minor", "patch"))
    if all(isinstance(p, int) for p in parts):
        return full, parts  # type: ignore[return-value]
    return full, env.parse_version(full)


def doc_path(doc, kind: str) -> str:
    """Absolute file path of an open DocumentSpecifier, best effort."""
    try:
        has_project = doc.HasField("project")
    except ValueError:
        has_project = False
    pdir = doc.project.path if has_project else ""
    pname = doc.project.name if has_project else ""
    if kind == "pcb":
        fn = doc.board_filename or (f"{pname}.kicad_pcb" if pname else "")
        if fn and not Path(fn).is_absolute() and pdir:
            return str(Path(pdir) / fn)
        return fn
    if kind == "schematic":
        return str(Path(pdir) / f"{pname}.kicad_sch") if pdir and pname else ""
    if kind == "project":
        return str(Path(pdir) / f"{pname}.kicad_pro") if pdir and pname else pdir
    return ""


def open_documents(kicad) -> tuple[list[dict[str, str]], list[str]]:
    """[{type, path}] for every document open in KiCad, plus probe notes for
    queries KiCad refused (e.g. DOCTYPE_SCHEMATIC on KiCad 9/10)."""
    from kipy.proto.common.types import DocumentType
    docs: list[dict[str, str]] = []
    notes: list[str] = []
    for kind, enum_name in DOC_QUERIES:
        try:
            found = kicad.get_open_documents(getattr(DocumentType, enum_name))
        except Exception as e:  # kipy ApiError on unsupported doc types
            notes.append(f"{kind}: {type(e).__name__}: {str(e)[:120]}")
            continue
        docs += [{"type": kind, "path": doc_path(d, kind)} for d in found]
    return docs, notes


def connect() -> dict:
    """Connect to a running KiCad GUI (no board required). Returns a dict with
    ipc_connected, kicad_version, open_documents, capabilities and the live
    `kicad` client — or {"ipc_connected": False, "reason": ...}."""
    try:
        from kipy import KiCad  # type: ignore
    except Exception as e:
        return {"ipc_connected": False,
                "reason": f"kicad-python (kipy) not importable in the server venv ({e})."}
    try:
        kicad = KiCad()
        full, ver = kicad_version_of(kicad)
    except Exception as e:
        return {"ipc_connected": False,
                "reason": f"KiCad IPC API not reachable ({e}). {ENABLE_API_HINT}"}
    docs, notes = open_documents(kicad)
    out = {"ipc_connected": True, "kicad": kicad, "kicad_version": full,
           "open_documents": docs, "capabilities": capabilities(ver)}
    return {**out, "open_documents_note": "; ".join(notes)} if notes else out


def public(k: dict) -> dict:
    """Strip live client objects so the dict can be returned to the caller."""
    return {kk: v for kk, v in k.items() if kk not in ("kicad", "board")}


def board_session() -> dict:
    """Connected KiCad + the board open in Pcbnew (keys `kicad`, `board`), or a
    JSON-safe dict describing why not (no `board` key)."""
    k = connect()
    if not k.get("ipc_connected"):
        return k
    try:
        board = k["kicad"].get_board()
    except Exception as e:
        return {**public(k), "ok": False,
                "reason": f"KiCad IPC is reachable but no board is open in Pcbnew ({e}). "
                          "Open the .kicad_pcb in the PCB editor and retry."}
    return {**k, "board": board}


def norm_path(path: str) -> str:
    """One path-resolution rule for every guard: absolute + normcase."""
    return os.path.normcase(os.path.abspath(path))


def holding_kind(k: dict, path: str) -> str | None:
    """Which editor kind ('pcb'/'schematic'/'project') in the connection dict
    `k` (from connect) has `path` open; None when it is not open or the
    connection failed (unknown never blocks)."""
    if not k.get("ipc_connected"):
        return None
    target = norm_path(path)
    for d in k["open_documents"]:
        if d["path"] and norm_path(d["path"]) == target:
            return d["type"]
    return None


def editor_holding(path: str) -> str | None:
    """Which KiCad editor kind has `path` open (fresh IPC connection), or None."""
    return holding_kind(connect(), path)


def open_in_editor(path: str) -> bool:
    """True when `path` is currently open in a running KiCad editor."""
    return editor_holding(path) is not None


def gui_can_edit_schematic(k: dict) -> bool:
    """True when the connected GUI is a KiCad 11 (schematic API): a schematic
    open there is edited THROUGH it instead of being refused."""
    return bool(k.get("ipc_connected")) and bool(k.get("capabilities", {}).get("schematic_api"))


def write_refusal(path: str, kind: str) -> dict:
    """The refusal every WRITE tool returns for a file open in a KiCad editor
    (writing it from outside would be lost or corrupt the editor's copy)."""
    return {"ok": False,
            "reason": f"{path} is open in KiCad {EDITOR_NAMES.get(kind, kind)}; close it "
                      "(or save+close) before writing, then reopen to see the result."}


def write_guard(path: str) -> dict | None:
    """Refusal dict when `path` is open in a KiCad editor, else None."""
    kind = editor_holding(path)
    return None if kind is None else write_refusal(path, kind)
