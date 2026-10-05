#!/usr/bin/env python3
"""The SWIG side of the stackup (ADR D7.5): copper layers and types on a pcbnew
board, the splice + fresh load + fill + save + verify step, the netlist /
artifact stackup lookup and the zone layers.

* ``apply_layers(board, rs)``: ``SetCopperLayerCount``, ``SetLayerType`` per
  copper role (signal / power / mixed) and ``SetBoardThickness`` (copper + plies
  + 2 masks). Every layer is checked enabled and typed afterwards.
* ``finalize(out_path, rs, fill=True)``: the saved board gets ``emit(rs)``
  spliced in (not for the legacy shorthand), is loaded fresh (which also loads
  ``<board stem>.kicad_dru``), filled, saved, and verified: stackup_kicad.compare
  (copper, types, thickness, block) plus ``zone_problems``. Never raises on a
  mismatch: ``ok`` is False and ``problems`` says why (a G3 blocker upstream).
* ``load_or_resolve(design, artifact_path=None)``: the netlist's stackup
  (stackup_model.resolve), cross-checked against ``<stem>.stackup.json`` when
  one is given (or named by $KICAD_EDA_STACKUP): a stale or unusable artifact
  is reported in ``note`` and the netlist wins. With ``design=None`` the
  artifact alone is read (tools that only receive a board).
* ``zone_layers(board)`` (layer via ``board.GetLayerName(z.GetLayer())`` and
  ``GetLayerSet()``, never ``ZONE.GetLayerName()``) and ``zone_problems``.

Runs under KiCad's bundled Python (9.0.4: Python 3.11; the 10.0.5 image:
/usr/bin/python3). Without pcbnew (the dev venv) only the pure helpers work.
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from types import MappingProxyType
from typing import Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import kicad_quiet  # noqa: E402,F401  (before pcbnew: no modal wx dialogs)

try:
    import pcbnew  # noqa: E402
except ImportError:          # the dev venv: pure helpers only
    pcbnew = None

import board_outline as bo  # noqa: E402
import stackup_kicad as sk  # noqa: E402
import stackup_model as sm  # noqa: E402

ARTIFACT_SCHEMA = "kicad-eda/physical@1"
ARTIFACT_ENV = "KICAD_EDA_STACKUP"
NET_ROLES = ("plane", "mixed")
_NESTED = frozenset({"copper", "dielectrics", "mask", "via_spans", "legacy", "warnings",
                     "decision_drafts"})


def _pcb() -> Any:
    if pcbnew is None:
        raise RuntimeError("pcbnew is not importable: this helper runs under KiCad's bundled "
                           "Python")
    return pcbnew


# ---------------------------------------------------------------------------
# Board layers (D7.5 step 1)
# ---------------------------------------------------------------------------
def _layer_type(kind: str) -> int:
    pcb = _pcb()
    types = {"signal": pcb.LT_SIGNAL, "power": pcb.LT_POWER, "mixed": pcb.LT_MIXED}
    if kind not in types:
        raise ValueError(f"no KiCad layer type for {kind!r} (signal | power | mixed)")
    return types[kind]


def apply_layers(board: Any, rs: sm.ResolvedStackup) -> dict[str, Any]:
    """Copper count, layer types and board thickness of ``rs`` on ``board``.
    RuntimeError when pcbnew does not take them (a copper layer missing,
    disabled, at an unexpected id or keeping another type)."""
    pcb = _pcb()
    board.SetCopperLayerCount(rs.kicad_copper_count)
    if board.GetCopperLayerCount() != rs.kicad_copper_count:
        raise RuntimeError(f"SetCopperLayerCount({rs.kicad_copper_count}) left "
                           f"{board.GetCopperLayerCount()} copper layers")
    for copper in rs.copper:
        lid = board.GetLayerID(copper.name)
        if lid != copper.kicad_id or not board.IsLayerEnabled(lid):
            raise RuntimeError(f"{copper.name} is not enabled at layer id {copper.kicad_id} "
                               f"(pcbnew gives id {lid})")
        board.SetLayerType(lid, _layer_type(copper.kicad_type))
        if board.GetLayerType(lid) != _layer_type(copper.kicad_type):
            raise RuntimeError(f"{copper.name} kept layer type {board.GetLayerType(lid)}, "
                               f"wanted {copper.kicad_type}")
    board.GetDesignSettings().SetBoardThickness(bo.nm(rs.board_thickness_computed))
    return {"copper": list(rs.copper_names()),
            "types": {c.name: c.kicad_type for c in rs.copper},
            "thickness_mm": rs.board_thickness_computed,
            "pcbnew": pcb.Version()}


# ---------------------------------------------------------------------------
# Zones
# ---------------------------------------------------------------------------
def zone_layers(board: Any) -> list[dict[str, Any]]:
    """Every zone: name, net, ``layer`` (board.GetLayerName(z.GetLayer()), None
    for a multi-layer zone without one), ``layers`` (its copper via
    GetLayerSet()), rule_area, filled."""
    out = []
    for zone in board.Zones():
        lid = zone.GetLayer()
        out.append({"name": zone.GetZoneName(), "net": zone.GetNetname(),
                    "layer": board.GetLayerName(lid) if lid >= 0 else None,
                    "layers": [board.GetLayerName(x) for x in zone.GetLayerSet().CuStack()],
                    "rule_area": bool(zone.GetIsRuleArea()), "filled": bool(zone.IsFilled())})
    return out


def _zone_label(zone: Mapping[str, Any]) -> str:
    name = zone.get("name") or ""
    return f"zone {name!r} ({zone.get('net') or 'no net'})" if name else \
        f"the {zone.get('net') or 'no-net'} zone"


def _pour_problem(rs: sm.ResolvedStackup, zone: Mapping[str, Any], layer: str) -> str | None:
    by_name = {c.name: c for c in rs.copper}
    copper = by_name.get(layer)
    if copper is None:
        return f"{_zone_label(zone)} is on {layer}, not a stackup copper layer"
    if copper.role == "unused":
        return (f"{_zone_label(zone)} is on {layer}, which is unused on a single-sided board "
                "(ADR D7.2.6: no copper there)")
    if copper.role == "plane" and zone.get("net") != copper.net:
        return (f"{_zone_label(zone)} is on {layer}, the {copper.net} plane: a plane layer "
                "carries one net")
    return None


def _pours(zones: Sequence[Mapping[str, Any]]) -> list[tuple[Mapping[str, Any], str]]:
    """(zone, copper layer) of every copper zone (rule areas left out)."""
    return [(z, layer) for z in zones if not z.get("rule_area") for layer in z.get("layers") or ()]


def zone_problems(rs: sm.ResolvedStackup, zones: Sequence[Mapping[str, Any]], *,
                  require_planes: bool = True) -> list[str]:
    """Copper zones (rule areas ignored) against the layer roles: none on an
    unused layer, only its own net on a plane layer and, with
    ``require_planes``, a pour of its net on every plane and mixed layer."""
    pours = _pours(zones)
    problems = [p for z, layer in pours if (p := _pour_problem(rs, z, layer))]
    nets = {c.name: c.net for c in rs.copper if c.role in NET_ROLES}
    covered = {layer for z, layer in pours if layer in nets and z.get("net") == nets[layer]}
    if require_planes:
        problems += [f"no {c.net} pour on the {c.role} layer {c.name}" for c in rs.copper
                     if c.role in NET_ROLES and c.net and c.name not in covered]
    return problems


# ---------------------------------------------------------------------------
# finalize (D7.5 steps 3-5)
# ---------------------------------------------------------------------------
def _read(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def finalize(out_path: str | os.PathLike, rs: sm.ResolvedStackup, fill: bool = True, *,
             require_planes: bool = True) -> dict[str, Any]:
    """Splice ``emit(rs)`` into the saved board (legacy: no block), LoadBoard it
    fresh, fill the zones (``fill``), SaveBoard, then verify: {ok, problems,
    spliced, filled, copper, thickness_mm, zones}. Raises only when pcbnew
    cannot load or save the file (or the text has no ``(setup``)."""
    pcb = _pcb()
    path = Path(out_path)
    spliced = rs.legacy is None
    if spliced:
        _write(path, sk.splice(_read(path), sk.emit(rs)))
    board = bo.load_board(path)
    board.BuildConnectivity()
    if fill:
        pcb.ZONE_FILLER(board).Fill(board.Zones())
    if pcb.SaveBoard(str(path), board) is False:
        raise RuntimeError(f"pcbnew.SaveBoard failed for {path}")
    saved = _read(path)
    zones = zone_layers(board)
    problems = sk.compare(rs, saved) + zone_problems(rs, zones, require_planes=require_planes)
    return {"ok": not problems, "problems": problems, "spliced": spliced, "filled": fill,
            "copper": list(sk.enabled_copper(saved)), "thickness_mm": sk.general_thickness(saved),
            "zones": zones}


# ---------------------------------------------------------------------------
# The stackup of a netlist or of its artifact
# ---------------------------------------------------------------------------
def stackup_from_dict(data: Mapping[str, Any]) -> sm.ResolvedStackup:
    """A ResolvedStackup from its ``to_dict()`` (the artifact's ``stackup``
    section); ValueError when the dict is not one (missing / unknown keys, or
    derived values such as kicad_copper_count that disagree)."""
    try:
        plain = {f.name: data[f.name] for f in fields(sm.ResolvedStackup)
                 if f.name not in _NESTED}
        legacy = data["legacy"]
        rs = sm.ResolvedStackup(
            **plain, copper=tuple(sm.CopperLayer(**c) for c in data["copper"]),
            dielectrics=tuple(sm.Dielectric(d["name"], d["kind"], bool(d["locked"]),
                                            tuple(sm.Ply(**p) for p in d["plies"]))
                              for d in data["dielectrics"]),
            mask=sm.MaskSpec(**data["mask"]),
            via_spans=tuple(tuple(s) for s in data["via_spans"]),
            legacy=None if legacy is None else MappingProxyType(dict(legacy)),
            warnings=tuple(data["warnings"]))
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"not a ResolvedStackup.to_dict() stackup ({type(exc).__name__}: "
                         f"{exc})") from exc
    if rs.to_dict() != dict(data):
        raise ValueError("the stackup dict is inconsistent (extra keys or derived values such "
                         "as kicad_copper_count / dielectric thickness disagree)")
    return rs


@dataclass(frozen=True)
class LoadedStackup:
    """``stackup`` and where it came from: ``source`` artifact | resolved,
    ``artifact`` the path consulted, ``note`` why an artifact was not used,
    ``issues`` the netlist's G1 stackup issues (empty for an artifact alone)."""
    stackup: sm.ResolvedStackup
    source: str
    artifact: str | None = None
    note: str | None = None
    issues: tuple[sm.Issue, ...] = ()


def read_artifact(path: str | os.PathLike) -> Mapping[str, Any]:
    """The physical-design artifact (schema kicad-eda/physical@1); ValueError
    when it is not one, OSError when it cannot be read."""
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if not isinstance(doc, Mapping) or doc.get("schema") != ARTIFACT_SCHEMA:
        raise ValueError(f"not a {ARTIFACT_SCHEMA} artifact")
    if not isinstance(doc.get("stackup"), Mapping):
        raise ValueError("the artifact has no stackup section")
    return doc


def _artifact_note(path: str, rs: sm.ResolvedStackup) -> str | None:
    if not os.path.isfile(path):
        return f"no stackup artifact at {path}: the stackup was resolved from the netlist"
    try:
        stored = stackup_from_dict(read_artifact(path)["stackup"])
    except (OSError, ValueError) as exc:
        return (f"stackup artifact {path} is unusable ({exc}): the stackup was resolved from "
                "the netlist")
    if stored.to_dict() != rs.to_dict():
        return (f"stackup artifact {path} is stale (its stackup differs from the netlist's): "
                "the stackup was resolved from the netlist; re-run physics_resolve")
    return None


def load_or_resolve(design: Mapping[str, Any] | None,
                    artifact_path: str | os.PathLike | None = None, *,
                    materials: Any = None, fabs: Any = None) -> LoadedStackup:
    """The stackup to build (see the module doc). ``artifact_path`` defaults to
    $KICAD_EDA_STACKUP. ValueError when the netlist stackup does not resolve
    (an ST-E1..E8 error; G1 reports it) or, without a design, when there is no
    usable artifact. Never mutates ``design``."""
    path = artifact_path if artifact_path is not None else os.environ.get(ARTIFACT_ENV)
    path = os.fspath(path) if path else None
    if design is None:
        if not path:
            raise ValueError("no netlist and no stackup artifact: pass the design or "
                             "<board stem>.stackup.json")
        return LoadedStackup(stackup_from_dict(read_artifact(path)["stackup"]), "artifact",
                             path)
    rs, issues = sm.resolve(design, materials=materials, fabs=fabs)
    if rs is None:
        errors = "; ".join(str(i) for i in issues if i.severity == "error")
        raise ValueError(f"the netlist stackup does not resolve: {errors}")
    note = _artifact_note(path, rs) if path else None
    source = "artifact" if path and note is None else "resolved"
    return LoadedStackup(rs, source, path, note, issues)
