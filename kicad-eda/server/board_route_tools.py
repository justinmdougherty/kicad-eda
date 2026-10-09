"""Agent-directed routing tools on the KiCad board API (backend "api").

The agent (Claude) routes the board itself: read what is still unrouted and
what is in the way, lay copper net by net, verify with KiCad's real
connectivity and kicad-cli DRC, repair, save. Freerouting (autoroute_board)
stays available for a KiCad 9-format board but is no longer the default.

    open_board / board_overview / board_pads / unrouted_connections    read
    route_net / add_vias / remove_routing                               write
    route_check (kicad-cli DRC) / route_status (the routed gate)        verify
    save_board / close_board / list_open_boards                         lifecycle

Every tool takes the board path first and opens it ON DEMAND; the session
registry and its policies (one headless board at a time, released_boards /
blocking_board, {ok: false, reason} instead of an exception, atexit close,
the D5 snapshot and stall history) live in board_sessions.py. Responses are
JSON dicts with ``ok``, ``backend: "api"``, ``mode`` and ``path``.

FILE FORMAT: the first save through a headless session rewrites the board in
KiCad 11 format. KiCad 9/10 tools (Freerouting, the SWIG gate checks) then
refuse it and kicad-cli 11 runs its DRC and exports (toolchain.board_cli picks
the writer's kicad-cli). ROADMAP D5: a pre-11 file is copied to X.k9.* first
(KiCad 11 GUI: at open), responses carry ``pre_k11_snapshot`` and route_check
flags ``stalled``. Units: millimetres everywhere, 4 decimals.
"""
from __future__ import annotations

import json
import math
import sys
import types
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import board_sessions as bs
import route_planner as rp
import toolchain as tc
from backends.errors import BoardApiError
from backends.pcb_api import BoardApi

DEFAULT_CLASS = "Default"      # nets in KiCad's default class carry no width binding (SWIG parity)
ROUND = 4
NO_CONNECTIVITY = ("KiCad's connectivity (GetConnectedItems) is unavailable on this session, so "
                   "what is still unrouted cannot be listed; route_check's unconnected_items say "
                   "what is open")
Item = Mapping[str, Any]

# the session registry (board_sessions); other modules call these names here
BACKEND, _key, _note, _with_board = bs.BACKEND, bs._key, bs._note, bs._with_board
release_for_rewrite, close_all, status = bs.release_for_rewrite, bs.close_all, bs.status


class _RegistryNames(types.ModuleType):
    """board_route_tools._BOARDS / _PROGRESS ARE board_sessions' registry:
    reading or rebinding them here (tests written before the F5-04 split
    monkeypatch these names) reads or rebinds board_sessions'."""
    _BOARDS = property(lambda self: bs._BOARDS,
                       lambda self, value: setattr(bs, "_BOARDS", value))
    _PROGRESS = property(lambda self: bs._PROGRESS,
                         lambda self, value: setattr(bs, "_PROGRESS", value))


sys.modules[__name__].__class__ = _RegistryNames


# ----------------------------------------------------------------------------
# board model helpers (dicts from pcb_items; pure)
# ----------------------------------------------------------------------------
def _named_nets(*groups: Iterable[Item]) -> list[str]:
    return sorted({str(item.get("net") or "") for group in groups for item in group} - {""})


def _of(items: Iterable[Item], net: str) -> list[Item]:
    return [item for item in items if item.get("net") == net]


def _by_net(items: Iterable[Item]) -> dict[str, list[Item]]:
    out: dict[str, list[Item]] = {}
    for item in items:
        if item.get("net"):
            out.setdefault(str(item["net"]), []).append(item)
    return out


def _outer_layers(copper: Sequence[str]) -> tuple[str, ...]:
    """The two outer copper layers (F.Cu, B.Cu) the agent routes on."""
    return tuple(copper) if len(copper) < 2 else (copper[0], copper[-1])


def _pad_bbox(pad: Item) -> list[float]:
    """Axis-aligned [x0, y0, x1, y1] of a (rotated) pad — the entry bbox the
    width gate measures its neck reach from."""
    angle = math.radians(float(pad.get("rotation") or 0.0))
    cos, sin = abs(math.cos(angle)), abs(math.sin(angle))
    w, h = float(pad.get("w") or 0.0), float(pad.get("h") or 0.0)
    hw, hh = (w * cos + h * sin) / 2.0, (w * sin + h * cos) / 2.0
    x, y = float(pad["x"]), float(pad["y"])
    return [x - hw, y - hh, x + hw, y + hh]


def _via_bbox(via: Item) -> list[float]:
    r = float(via.get("diameter") or 0.0) / 2.0
    x, y = float(via["x"]), float(via["y"])
    return [x - r, y - r, x + r, y + r]


def _clusters(api: BoardApi, nets: Sequence[str]) -> tuple[dict[str, list[list[str]]] | None,
                                                           str | None]:
    """{net: KiCad's connected pad groups} or (None, why connectivity is
    unavailable). Any other API error propagates."""
    if not api.connectivity_available:
        return None, f"{NO_CONNECTIVITY} (KiCad {api.kicad_version})"
    out: dict[str, list[list[str]]] = {}
    for net in nets:
        try:
            out[net] = api.clusters(net)
        except BoardApiError as exc:
            if api.connectivity_available:
                raise
            return None, str(exc)
    return out, None


def _netclass(api: BoardApi, net: str) -> dict:
    """The net's resolved class (mm); an unknown net names the valid ones."""
    if not isinstance(net, str) or not net.strip():
        raise BoardApiError(f"net must be a non-empty net name, got {net!r}")
    classes = api.netclasses()
    if net not in classes:
        raise BoardApiError(f"no net {net!r} on {api.path}; nets: {sorted(classes)}")
    return classes[net]


def _check_net(api: BoardApi, net: str) -> None:
    if net and net not in api.nets():
        raise BoardApiError(f"no net {net!r} on {api.path}; nets: {api.nets()}")


def _read_netlist(netlist: str) -> dict:
    """The netlist.json the board was built from ({} when none is given)."""
    if not netlist:
        return {}
    try:
        design = json.loads(Path(netlist).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BoardApiError(f"netlist {netlist} unreadable ({exc}); pass the netlist.json the "
                            "board was built from, or omit it") from exc
    if not isinstance(design, dict):
        raise BoardApiError(f"netlist {netlist} must hold a JSON object")
    return design


# ----------------------------------------------------------------------------
# reads
# ----------------------------------------------------------------------------
def _opened(api: BoardApi) -> dict:
    return {**api.overview(), "opened": True, "kicad_version": api.kicad_version,
            "connectivity_available": api.connectivity_available, **tc.snapshot_field(api.path)}


def _overview(api: BoardApi) -> dict:
    over = api.overview()
    pads, tracks, vias = api.pads(), api.tracks(), api.vias()
    clusters, note = _clusters(api, _named_nets(pads))
    routing = {"tracks": len(tracks), "vias": len(vias),
               "length": round(sum((float(t["length"]) for t in tracks), 0.0), ROUND),
               "nets_with_copper": len(_named_nets(tracks, vias))}
    unrouted = None if clusters is None else len(rp.connections(pads, clusters))
    return {**over, "routing": routing, "unrouted": unrouted, **tc.snapshot_field(api.path),
            "connectivity_available": clusters is not None, **_note(note)}


def _pads(api: BoardApi, net: str, ref: str) -> dict:
    found = api.pads()
    _check_net(api, net)
    refs = sorted({str(p["ref"]) for p in found if p.get("ref")})
    if ref and ref not in refs:
        raise BoardApiError(f"no footprint {ref!r} on {api.path}; references: {refs}")
    pads = [p for p in found if (not net or p["net"] == net) and (not ref or p["ref"] == ref)]
    return {"pads": pads, "count": len(pads)}


def _with_obstacles(api: BoardApi, conns: Sequence[Item], pads: Sequence[Item],
                    clearance: float) -> list[dict]:
    """Each connection plus {layer: obstacles} on the outer copper layers."""
    tracks, vias, classes = api.tracks(), api.vias(), api.netclasses()
    layers = _outer_layers(api.copper_layers())
    out = []
    for conn in conns:
        cls = classes.get(conn["net"], {})
        gap = clearance or float(cls.get("clearance") or 0.0)
        width = float(cls.get("track_width") or 0.0)
        out.append({**conn, "obstacles": rp.corridor(conn, layers, pads=pads, tracks=tracks,
                                                     vias=vias, clearance=gap, width=width)})
    return out


def _unrouted(api: BoardApi, net: str, with_obstacles: bool, clearance: float) -> dict:
    if clearance < 0:
        raise ValueError(f"clearance must be >= 0 mm, got {clearance}")
    pads = api.pads()
    _check_net(api, net)
    wanted = [p for p in pads if not net or p["net"] == net]
    clusters, note = _clusters(api, _named_nets(wanted))
    if clusters is None:
        return {"unrouted": None, "connections": [], "connectivity_available": False,
                "note": note}
    conns = rp.connections(wanted, clusters)
    if with_obstacles and conns:
        conns = _with_obstacles(api, conns, pads, clearance)
    return {"unrouted": len(conns), "connections": conns, "connectivity_available": True}


# ----------------------------------------------------------------------------
# writes
# ----------------------------------------------------------------------------
def _add_vias_after(api: BoardApi, net: str, vias: Sequence[dict], tracks: Sequence[dict]) -> list:
    """The via step of route_net; on failure the reason says how to undo the
    tracks already laid."""
    if not vias:
        return []
    try:
        return api.add_vias(net, vias)
    except BoardApiError as exc:
        ids = [t["id"] for t in tracks]
        raise BoardApiError(f"{exc}; the {len(ids)} track(s) of this path were already added: "
                            f"remove_routing(ids={ids}) undoes them") from exc


def _route_net(api: BoardApi, net: str, path: Sequence[Item], width: float, allow_narrow: bool,
               via_diameter: float, via_drill: float) -> dict:
    netclass = _netclass(api, net)
    plan = rp.validate_path(path, netclass, api.rules(), width, allow_narrow,
                            via_diameter, via_drill)
    tracks = api.add_tracks(net, plan["segments"])
    vias = _add_vias_after(api, net, plan["vias"], tracks)
    return {"net": net, "created": {"tracks": len(tracks), "vias": len(vias)},
            "ids": [t["id"] for t in tracks] + [v["id"] for v in vias],
            "length": rp.route_length(plan["segments"]), "warnings": list(plan["warnings"])}


def _xy(value: Any, where: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{where} must be [x, y] in mm, got {value!r}")
    try:
        x, y = float(value[0]), float(value[1])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{where} must be [x, y] in mm, got {value!r}") from exc
    if not (math.isfinite(x) and math.isfinite(y)):
        raise ValueError(f"{where} must be finite, got {value!r}")
    return x, y


def _at_least(value: float, minimum: Any, what: str) -> None:
    if minimum and value + rp.WIDTH_TOL_MM < float(minimum):
        raise ValueError(f"{what} {round(value, ROUND)} mm is below the board minimum "
                         f"{minimum} mm")


def _via_specs(points: Any, diameter: float, drill: float, netclass: Item,
               rules: Item) -> list[dict]:
    """[{x, y, diameter, drill}] for add_vias: sizes default to the net class,
    then the board minimums (the same rules validate_path applies to a via)."""
    if not isinstance(points, (list, tuple)) or not points:
        raise ValueError("points must be a non-empty list of [x, y] in mm")
    d = float(diameter or 0) or netclass.get("via_diameter") or rules.get("min_via_diameter") or 0
    h = float(drill or 0) or netclass.get("via_drill") or rules.get("min_via_drill") or 0
    if d <= 0 or h <= 0:
        raise ValueError("no via size: pass diameter and drill (the net class and the board "
                         "rules give none)")
    if h >= d:
        raise ValueError(f"drill {h} mm >= diameter {d} mm; the drill must leave an annular ring")
    _at_least(d, rules.get("min_via_diameter"), "via diameter")
    _at_least(h, rules.get("min_via_drill"), "via drill")
    _at_least((d - h) / 2.0, rules.get("min_via_annular"), "annular ring")
    spots = [_xy(p, f"points[{i}]") for i, p in enumerate(points)]
    return [{"x": round(x, ROUND), "y": round(y, ROUND), "diameter": round(d, ROUND),
             "drill": round(h, ROUND)} for x, y in spots]


def _add_vias(api: BoardApi, net: str, points: Any, diameter: float, drill: float) -> dict:
    specs = _via_specs(points, diameter, drill, _netclass(api, net), api.rules())
    vias = api.add_vias(net, specs)
    return {"net": net, "created": {"tracks": 0, "vias": len(vias)},
            "ids": [v["id"] for v in vias], "vias": vias}


def _remove(api: BoardApi, net: str, ids: Sequence[str] | None) -> dict:
    wanted = list(dict.fromkeys(str(i) for i in (ids or [])))
    if not net and not wanted:
        raise ValueError("give net (strips every track and via of that net) and/or ids (from "
                         "route_net / add_vias / the board's tracks and vias)")
    removed = {"tracks": 0, "vias": 0}
    if wanted:
        track_ids = {t["id"] for t in api.tracks()}
        api.remove(wanted)
        n_tracks = sum(1 for i in wanted if i in track_ids)
        removed = {"tracks": n_tracks, "vias": len(wanted) - n_tracks}
    if net:
        stripped = api.remove_net_routing(net)
        removed = {kind: removed[kind] + int(stripped.get(kind, 0)) for kind in removed}
    return {"removed": removed, "net": net, "ids": wanted}


# ----------------------------------------------------------------------------
# verify: kicad-cli DRC and the routed gate
# ----------------------------------------------------------------------------
def _route_check(api: BoardApi, refill: bool, save: bool, max_items: int) -> dict:
    if max_items < 0:
        raise ValueError(f"max_items must be >= 0, got {max_items}")
    out: dict = {}
    if refill:                  # a refill marks the board modified: save=true then rewrites it
        out["zones_refilled"] = api.refill_zones()
    if save and api.is_modified:
        out.update(bs._save(api), saved=True)
    else:                       # save=false, or nothing changed (possible only with refill=false)
        out["saved"] = False
        if api.is_modified:
            out["unsaved_note"] = ("the board has unsaved changes; DRC judged the file on disk "
                                   "(pass save=true or call save_board first)")
        elif save:
            out["save_note"] = ("nothing to save: the board had no unsaved changes, so the file "
                                "on disk was left as it is")
    if not tc.board_cli(api.path):
        raise BoardApiError("kicad-cli not found; install KiCad (or set KICAD_PATH): route_check "
                            "needs it to run DRC")
    drc = {**out, **tc.drc_check(api.path, max_items), **tc.snapshot_field(api.path)}
    return {**drc, **bs._record_round(api, drc)}


def _net_status(net: str, pads: Sequence[Item], tracks: Sequence[Item], vias: Sequence[Item],
                clusters: Sequence[Sequence[str]] | None, netclass: Item | None,
                preroute_width: float | None,
                preroute_authored: float | None) -> tuple[dict, list[str]]:
    """One net's row of route_status and its width issues. Only a net bound
    to a non-default class is policed for width (SWIG gate parity)."""
    cls = dict(netclass or {})
    class_width = cls.get("track_width")
    bound = bool(class_width) and (cls.get("name") or DEFAULT_CLASS) != DEFAULT_CLASS
    entries = [_pad_bbox(p) for p in pads] + [_via_bbox(v) for v in vias]
    authored = {net: preroute_authored} if preroute_authored is not None else None
    issues = rp.width_compliance({net: tracks}, {net: class_width} if bound else {},
                                 {net: entries}, {net: preroute_width} if preroute_width else None,
                                 authored)
    widths = [float(t["width"]) for t in tracks]
    row = {"pads": len(pads), "clusters": None if clusters is None else len(clusters),
           "complete": None if clusters is None else len(clusters) <= 1,
           "tracks": len(tracks), "vias": len(vias),
           "length": round(sum((float(t["length"]) for t in tracks), 0.0), ROUND),
           "min_width": min(widths) if widths else None,
           "class": cls.get("name") or DEFAULT_CLASS, "class_width": class_width,
           "width_ok": not issues}
    return row, issues


def _ampacity_issues(design: Item, pads: Sequence[Item], vias: Sequence[Item]) -> list[str]:
    specs = design.get("net_specs") or {}
    if not isinstance(specs, dict) or not specs:
        return []
    pth: dict[str, list[float]] = {}
    for pad in pads:
        if pad.get("through_hole") and pad.get("drill") and pad.get("net"):
            pth.setdefault(str(pad["net"]), []).append(float(pad["drill"]))
    return rp.via_ampacity(_by_net(vias), pth, specs, list(design.get("preroutes") or []))


def _route_status(api: BoardApi, netlist: str) -> dict:
    design = _read_netlist(netlist)
    pads, tracks, vias, classes = api.pads(), api.tracks(), api.vias(), api.netclasses()
    nets = _named_nets(pads, tracks, vias)
    clusters, note = _clusters(api, nets)
    pre_w, pre_len = rp.preroutes(design)
    rows: dict[str, dict] = {}
    width_issues: list[str] = []
    for net in nets:
        row, issues = _net_status(net, _of(pads, net), _of(tracks, net), _of(vias, net),
                                  None if clusters is None else clusters.get(net, []),
                                  classes.get(net), pre_w.get(net), pre_len.get(net))
        rows[net] = row
        width_issues.extend(issues)
    unrouted = None if clusters is None else len(rp.connections(pads, clusters))
    complete = None if unrouted is None else unrouted == 0
    ampacity = _ampacity_issues(design, pads, vias)
    return {"nets": rows, "unrouted": unrouted, "complete": complete,
            "width_issues": width_issues, "via_ampacity_issues": ampacity,
            "passed": bool(complete) and not width_issues and not ampacity,
            "connectivity_available": clusters is not None,
            **_note(note), **_note(api.notes, "notes")}


def _diff_pair_warning(netlist: str) -> list[str]:
    """The SWIG routed gate also measures diff-pair coupling; this backend
    does not, so a netlist declaring pairs gets a warning instead of silence."""
    try:
        specs = _read_netlist(netlist).get("net_specs") or {}
    except BoardApiError:
        return []
    pairs = sorted(n for n, s in specs.items() if isinstance(s, dict) and s.get("diff_pair"))
    if not pairs:
        return []
    return [f"diff pair(s) {pairs}: length match and coupling are not checked on the board "
            f"API (the SWIG gate's check_diff_pairs only); verify them by inspection"]


# ----------------------------------------------------------------------------
# TOOLS
# ----------------------------------------------------------------------------
def open_board(pcb: str, mode: str = "auto") -> dict:
    """Open a .kicad_pcb for agent routing and return its overview: copper
    layers (top to bottom), outline, rules (board minimums, mm), netclasses
    ({net: {name, clearance, track_width, via_diameter, via_drill}}), planes
    (filled inner-layer pours), footprints and counts, plus {opened, mode,
    kicad_version, connectivity_available}. mode "auto" joins a running KiCad
    GUI that has the board open, else launches a headless KiCad 11
    api-server with the file preloaded; "gui" / "headless" force one. Every
    other tool opens on demand, so this call is optional: use it to choose
    the mode or to re-read the file. A board that is already open with
    unsaved changes is not re-opened (save_board or close_board it first).
    A KiCad 11 GUI session copies a KiCad 9/10 file to X.k9.* at once
    (pre_k11_snapshot_taken): its user may save at any time. On {ok: false}
    the reason names the fix: install a KiCad 11 nightly (or open the board
    in a KiCad GUI), save/close the dirty headless board that blocks the
    single server, or check the path."""
    return bs._reopen(pcb, mode, _opened)


def board_overview(pcb: str) -> dict:
    """The board as the agent needs it before routing (open_board's overview)
    plus routing: {tracks, vias, length, nets_with_copper}, unrouted (the
    number of pad-group links still to make, null when KiCad's connectivity
    is unavailable: see note), connectivity_available and pre_k11_snapshot
    (X.k9.kicad_pcb, if any). Start here, then unrouted_connections."""
    return _with_board(pcb, _overview)


def board_pads(pcb: str, net: str = "", ref: str = "") -> dict:
    """{pads: [{id, ref, pad, net, x, y, layers, through_hole, shape, w, h,
    rotation, drill}], count} in mm; net and/or ref narrow the list (an
    unknown net or reference is refused with the valid ones). Pad ids are
    what unrouted_connections and route_status cluster by."""
    return _with_board(pcb, lambda api: _pads(api, net, ref))


def unrouted_connections(pcb: str, net: str = "", with_obstacles: bool = True,
                         clearance: float = 0.0) -> dict:
    """The work list: {unrouted: n, connections: [{net, from: pad, to: pad,
    length, obstacles: {"F.Cu": [...], "B.Cu": [...]}}], connectivity_available}.
    For every net with >= 2 pad groups (KiCad's real connectivity: tracks,
    vias and filled planes count) the N-1 closest pad pairs that join the
    groups, sorted by net then length. Each obstacle is foreign copper near
    the straight line from -> to on that outer layer: {kind: pad|track|via,
    id, ref, pad, net, distance (mm from the line's centre to its copper
    edge, negative = on the line), t (0..1 along the line)}; judged at the
    net's class clearance (or `clearance`) + half the class track width.
    Pass net= to see one net. Without connectivity (KiCad < 10.0.1)
    unrouted is null and note says to use route_check's unconnected items."""
    return _with_board(pcb, lambda api: _unrouted(api, net, with_obstacles, clearance))


def route_net(pcb: str, net: str, path: list[dict], width: float = 0.0,
              allow_narrow: bool = False, via_diameter: float = 0.0,
              via_drill: float = 0.0) -> dict:
    """Lay copper on `net` along `path`: a list of polylines {"layer": "F.Cu",
    "points": [[x, y], ...], "width"?: mm} and vias {"via": [x, y],
    "diameter"?, "drill"?} in order. Two polylines must be joined by a via
    placed exactly at the end of the first / start of the next; a leading or
    trailing via is a stub to a plane. width 0 = the net class track width
    (below it is refused unless allow_narrow, for a short pad-entry neck;
    below the board minimum always refused); via sizes 0 = the class values,
    then the board minimums. Returns {created: {tracks, vias}, ids, length,
    warnings} (non-45-degree bends, acute turns and same-layer vias warn but
    are laid). A path that fails validation adds nothing and the reason
    names the element to fix; if KiCad refuses the vias after the tracks
    were laid, the reason lists the track ids for remove_routing(ids=).
    Batch 3-6 nets, then route_check."""
    return _with_board(pcb, lambda api: _route_net(api, net, path, width, allow_narrow,
                                                   via_diameter, via_drill))


def add_vias(pcb: str, net: str, points: list[list[float]], diameter: float = 0.0,
             drill: float = 0.0) -> dict:
    """Through vias on `net` at points [[x, y], ...] (mm): a plane stitch or
    a layer change laid apart from route_net. diameter / drill 0 = the net
    class values, then the board minimums; the drill must keep the minimum
    annular ring. Returns {created: {tracks: 0, vias}, ids, vias}; on
    {ok: false} nothing was added (fix the size or the net name)."""
    return _with_board(pcb, lambda api: _add_vias(api, net, points, diameter, drill))


def remove_routing(pcb: str, net: str = "", ids: list[str] | None = None) -> dict:
    """Undo copper: net= strips every track and via of that net (the way to
    re-route a net after a DRC violation), ids= removes specific tracks /
    vias (from route_net, add_vias or the board). One of them is required.
    Returns {removed: {tracks, vias}}. An id that is not a track or via on
    the board refuses the whole call and removes nothing."""
    return _with_board(pcb, lambda api: _remove(api, net, ids))


def route_check(pcb: str, refill: bool = True, save: bool = True, max_items: int = 40) -> dict:
    """Verify with KiCad: refill the zones, save the board when it has
    unsaved changes (kicad-cli reads the file; the first headless save
    writes KiCad 11 format, after copying a KiCad 9/10 file to X.k9.*) and
    run kicad-cli DRC at error severity with the kicad-cli of the KiCad that
    wrote the file. The refill counts as a change, so with the defaults even
    an untouched board is saved (headless: rewritten in KiCad 11 format);
    only refill=false leaves an unmodified board as it is (saved false +
    save_note). Returns
    {ok: errors == 0 and unconnected == 0, errors, unconnected, types,
    violations: [{type, severity, description, items: [{description, x,
    y}]}] (first max_items), unconnected_items: [same shape; an item names a
    pad "Pad 1 [NET] of C1 on F.Cu"], kicad_cli, report, saved,
    zones_refilled, progress: {rounds, score (errors + unconnected), best,
    rounds_without_improvement}, stalled}. Repair loop: per violation read
    its items' x, y, remove_routing(net=...) the offending net, re-route,
    run again; stalled (3 rounds without a new best) = stop, stall_note
    names the fallback. save=false keeps the file (unsaved_note: DRC judged
    the disk copy). {ok: false, reason} without counts: DRC itself failed (no
    kicad-cli; it exited non-zero, crashed or wrote no report: the last
    report is deleted first, never re-read). A round that is not counted
    (unsaved copper, failed DRC) says so in progress_note and still returns
    the progress / stalled verdict so far."""
    return _with_board(pcb, lambda api: _route_check(api, refill, save, max_items))


def route_status(pcb: str, netlist: str = "") -> dict:
    """The routed gate on the board API: {nets: {net: {pads, clusters,
    complete, tracks, vias, length, min_width, class, class_width,
    width_ok}}, unrouted, complete, width_issues, via_ampacity_issues,
    passed}. complete = every net is one connected group (KiCad's
    connectivity, planes included); width_issues = the SWIG gate's rule: a
    net bound to a net class routed below its width in the open field (a
    short neck into a narrower pad or via is exempt); via_ampacity_issues =
    IPC-2221 barrel ampacity of >= 2 A nets from the netlist's net_specs
    (empty without a netlist). passed = complete and no issues: only then
    export_fab_package. Without connectivity complete is null and passed
    false (use route_check's unconnected count)."""
    return _with_board(pcb, lambda api: _route_status(api, netlist))


def routed_gate(pcb: str, netlist: str = "") -> dict:
    """check_design_gates' stage "routed" for a board the pcbnew install
    cannot read (KiCad 11 format): route_status folded into the SWIG gate's
    shape {ok, stage: "routed", passed, issues, warnings, backend: "api"}.
    issues = width + via-ampacity issues + the unrouted count; warnings =
    connectivity / diff-pair notes."""
    res = route_status(pcb, netlist)
    base = {"stage": "routed", "backend": BACKEND, "path": res.get("path", pcb)}
    if not res.get("ok"):
        return {**base, "ok": False, "passed": False, "issues": [], "warnings": [],
                "reason": res.get("reason")}
    issues = list(res["width_issues"]) + list(res["via_ampacity_issues"])
    if res["unrouted"]:
        open_nets = sorted(n for n, row in res["nets"].items() if row["complete"] is False)
        issues.append(f"{res['unrouted']} connection(s) still unrouted on {open_nets}")
    warnings = ([res["note"]] if res.get("note") else []) + _diff_pair_warning(netlist)
    return {**base, "ok": True, "passed": res["passed"], "issues": issues, "warnings": warnings,
            "unrouted": res["unrouted"], "complete": res["complete"]}


def save_board(pcb: str) -> dict:
    """Write the open board to disk and clear its unsaved flag: {saved,
    path}. A headless session writes KiCad 11 format (KiCad 9/10 tools then
    refuse the file; kicad-cli 11 checks and exports it), after copying a
    KiCad 9/10 file to X.k9.* (pre_k11_snapshot_taken). Refused when the
    board is not open or that copy fails (nothing saved)."""
    return bs._save_open(pcb)


def close_board(pcb: str, save: bool = True) -> dict:
    """Close the board's session (stops a headless api-server; leaves a GUI
    board in its editor): {closed, saved, discarded_changes}. save=true
    writes it first when it has unsaved changes (an untouched board is not
    rewritten). save=false on a HEADLESS session discards the unsaved copper
    (the file on disk is untouched: discarded_changes true); on a GUI
    session the copper STAYS in the running KiCad editor (discarded_changes
    false + note: save or undo it there). Refused when the board is not
    open, or when the save (or its X.k9.* copy) fails: the board stays open."""
    return bs._close(pcb, save)


def list_open_boards() -> dict:
    """The boards this server holds: {boards: [{path, mode, unsaved_changes,
    pre_k11_snapshot?}]}. A headless api-server holds one board; a "gui"
    entry is a board open in a running KiCad."""
    return {"ok": True, "backend": BACKEND, "boards": bs._open_list()}


TOOLS = (open_board, board_overview, board_pads, unrouted_connections, route_net, add_vias,
         remove_routing, route_check, route_status, save_board, close_board, list_open_boards)
