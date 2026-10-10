#!/usr/bin/env python3
"""Headless board builder — runs under KiCad's bundled Python (has `pcbnew`).

Reads a netlist JSON (components + pad-level nets) and produces a .kicad_pcb with
every footprint loaded from the library, placed, and every pad assigned to its net.
This is the path the IPC API cannot do (it cannot instantiate library footprints),
so the server shells out to KiCad's interpreter to run this.

Usage:
  <kicad-python> swig_build_board.py <netlist.json> <out.kicad_pcb> <footprints> \
                                     [board_w_mm] [board_h_mm] [placement.json]
<footprints> is a directory of stock <nick>.pretty libraries (back-compat) or a
*.json library map (libtable.LibraryResolver.library_map(): the netlist
libraries block, the project fp-lib-table, then the stock directory; D1). Every
footprint loaded, stock parts and mounting holes included, carries its library
nickname (SetFPID(LIB_ID(nick, name))). A footprint this pcbnew does not load
(e.g. a newer dialect, D0) fails with fp_dialect's message: the .kicad_mod, its
(version N), pcbnew.GetBuildVersion() and the newest format that build loads.
board_w_mm / board_h_mm <= 0 mean "not given" (an outline board's size is its
outline bbox; a legacy fully placed board without a board block is 50 x 25).

Board outline (ROADMAP_DECISIONS D2, F3-11; build_features has the details):
env KICAD_EDA_OUTLINE names an outline_resolve sidecar whose board block
replaces the netlist's; else the netlist board block. A board with
outline.loop / corner_radius / cutouts / datum gets exact Edge.Cuts
(board_outline.draw_outline), the aux + grid origin at its datum (default:
bbox bottom-left), planes that follow the outline and the smallest landscape
sheet that holds it (A4..A0, else User). An unresolved outline.dxf is refused.
Plain w/h and auto-sized boards keep the legacy geometry exactly. KIIDs are
seeded from sha256(netlist + sidecar), so rebuilds save the same file order.

Placement (F3-12; build_layout has the details): x/y parts by courtyard
centre, D3 "at": [x, y, rot] parts by footprint origin and final rotation (F
and B side); auto parts pack into board_field fields (legacy w/h boards: the
legacy fields; outline boards: auto_fields). A component "edge" puts a
connector on that board edge and snaps it onto the outline, hanging its
"overhang" (else the overhang derived from an edge-mount footprint's 'PCB Edge'
marker) past it. Every courtyard is checked against the outline.
Emits a JSON result on stdout; F3-12 adds "anchors" (the "at" parts), "fields"
([x0, y0, x1, y1] per auto field), "containment" (outline_audit issues, also
warnings) and "derived_overhang" ({ref: {overhang_mm, source}}).
"""
import functools
import json
import os
import sys
import traceback
from collections.abc import Callable
from pathlib import Path

import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew

import board_outline as bo
import build_features as bf
import build_layout as bl
import edge_mount as em
import fp_dialect
import libtable
import outline as ol


def mm(v: float) -> int:
    return pcbnew.FromMM(float(v))


def vec(x: float, y: float) -> "pcbnew.VECTOR2I":
    return pcbnew.VECTOR2I(mm(x), mm(y))


def footprint_libraries(arg: str) -> str | dict:
    """argv[3]: a stock footprints directory, returned as is (back-compat), or
    a *.json library map, returned as the dict libtable.lookup() reads."""
    if not arg.lower().endswith(".json"):
        return arg
    try:
        with open(arg, encoding="utf-8-sig") as f:
            lib_map = json.load(f)
    except (OSError, ValueError) as e:
        raise RuntimeError(f"cannot read the library map {arg}: {e}") from e
    key = libtable.MAP_KEYS[libtable.FOOTPRINT]
    if not isinstance(lib_map, dict) or not isinstance(lib_map.get(key, {}), dict):
        raise RuntimeError(f"{arg} is not a library map (libtable.LibraryResolver."
                           f"library_map(): a JSON object with a '{key}' object)")
    if lib_map.get("version") != libtable.MAP_VERSION:
        raise RuntimeError(f"library map {arg} has version {lib_map.get('version')!r}; this "
                           f"builder reads version {libtable.MAP_VERSION}")
    return lib_map


def library_dir(footprints: str | dict, nick: str) -> str:
    """The .pretty directory of library ``nick``: from the library map (its
    entry, else its stock directory) or ``<footprints dir>/<nick>.pretty``."""
    if not isinstance(footprints, dict):
        return f"{footprints}/{nick}.pretty"
    path = libtable.lookup(footprints, libtable.FOOTPRINT, nick)
    if path is None:
        stock = (footprints.get("stock") or {}).get(libtable.MAP_KEYS[libtable.FOOTPRINT])
        raise RuntimeError(f"footprint library '{nick}' is not in the library map (netlist "
                           f"libraries block, project fp-lib-table), or its row is disabled, "
                           f"and the stock footprints ({stock}) have no {nick}.pretty")
    return path


def load_footprint(footprints: str | dict, lib_id: str) -> "pcbnew.FOOTPRINT":
    """Load ``nick:name`` from its library (library_dir) and stamp the
    nickname on it: SetFPID(LIB_ID(nick, name)), checked by reading it back
    with GetUniStringLibId() (never LIB_ID.Format()). A footprint pcbnew does
    not load raises RuntimeError with fp_dialect.load_failure_message."""
    nick, _, name = lib_id.partition(":")
    if not nick or not name:
        raise RuntimeError(f"footprint {lib_id!r} is not '<library>:<footprint>'")
    libpath = library_dir(footprints, nick)
    mod = os.path.normpath(os.path.join(libpath, name + ".kicad_mod"))
    build = pcbnew.GetBuildVersion()
    fp = None
    if os.path.isdir(libpath):          # a missing library makes FootprintLoad raise
        try:
            fp = pcbnew.FootprintLoad(libpath, name)
        except Exception as e:          # KiCad's IO_ERROR on a file it cannot parse
            raise RuntimeError(f"{fp_dialect.load_failure_message(mod, build, lib_id)} "
                               f"({type(e).__name__}: {e})") from e
    if fp is None:
        raise RuntimeError(fp_dialect.load_failure_message(mod, build, lib_id))
    fp.SetFPID(pcbnew.LIB_ID(nick, name))
    stamped = str(fp.GetFPID().GetUniStringLibId())
    if stamped != f"{nick}:{name}":
        raise RuntimeError(f"SetFPID(LIB_ID({nick!r}, {name!r})) reads back as {stamped!r} "
                           f"in pcbnew {build}")
    return fp


from place_engine import fp_extent, fp_mouth  # noqa: E402

DEFAULT_SIZE_MM = (50.0, 25.0)    # a fully placed legacy board with no board block
MARKER_LAYERS = (("Dwgs_User", "Dwgs.User"), ("Cmts_User", "Cmts.User"))   # edge_mount's

# JLCPCB-class fab floors (mm), applied to the board design settings.
# The via drill floor is ``m_MinThroughDrill`` (KiCad >= 9); the pre-9 field
# ``m_ViasMinDrill`` is deliberately absent so a current pcbnew applies every
# rule with no "skipped" warning.
FAB_RULES_MM = {"m_MinClearance": 0.15, "m_TrackMinWidth": 0.15,
                "m_ViasMinSize": 0.45, "m_MinThroughDrill": 0.2,
                "m_MicroViasMinSize": 0.2, "m_MicroViasMinDrill": 0.15,
                "m_CopperEdgeClearance": 0.3}


def apply_design_rules(board: "pcbnew.BOARD", rules_mm: dict = FAB_RULES_MM) -> list[str]:
    """Set the fab floors on the board's design settings. Returns the list of
    fields this pcbnew does not expose so the caller can report them; setattr
    on a missing SWIG field would silently create a dead Python attribute, so
    those are skipped, never faked. Raises RuntimeError if the settings object
    exposes none of the fields or a set fails."""
    ds = board.GetDesignSettings()
    skipped = [attr for attr in rules_mm if not hasattr(ds, attr)]
    if len(skipped) == len(rules_mm):
        raise RuntimeError(f"design settings expose none of {sorted(rules_mm)} "
                           f"in this pcbnew ({pcbnew.GetBuildVersion()}); "
                           f"fab rules not applied")
    for attr, val in rules_mm.items():
        if attr in skipped:
            continue
        try:
            setattr(ds, attr, pcbnew.FromMM(val))
        except Exception as e:
            raise RuntimeError(f"cannot set design rule {attr}={val} mm: "
                               f"{type(e).__name__}: {e}") from e
    return skipped


def argv_dims(argv: list[str]) -> tuple[float, float]:
    """argv[4] / argv[5] (mm); absent or <= 0 means 'not given' (DEFAULT_SIZE_MM)."""
    dims = []
    for i, default in ((4, DEFAULT_SIZE_MM[0]), (5, DEFAULT_SIZE_MM[1])):
        value = float(argv[i]) if len(argv) > i else default
        dims.append(value if value > 0 else default)
    return dims[0], dims[1]


def read_design(netlist_path: str, placement_arg: str | None) -> tuple[bytes, dict]:
    """(netlist bytes, design) with the placement overrides (ref -> [x, y,
    rot], argv[6]) merged into NEW component dicts (bl.apply_overrides: an
    override replaces a part's "at")."""
    raw = Path(netlist_path).read_bytes()
    design = json.loads(raw.decode("utf-8"))
    if not placement_arg or placement_arg in ("", "-"):
        return raw, design
    with open(placement_arg, encoding="utf-8") as f:
        placement = json.load(f)
    return raw, bl.apply_overrides(design, placement)


def load_parts(design: dict, footprints: str | dict) -> tuple[dict, dict, dict, dict]:
    """Every component's footprint loaded (reference + value set) and measured
    at the origin: (fps, extents (w, h, cx_off, cy_off) at rot 0, pad counts,
    connector mouths), each keyed by ref."""
    fps, extents, pincount, mouth = {}, {}, {}, {}
    for c in design["components"]:
        fp = load_footprint(footprints, c["footprint"])
        fp.SetReference(c["ref"])
        fp.SetValue(c.get("value", ""))
        fps[c["ref"]] = fp
        extents[c["ref"]] = fp_extent(fp)
        pincount[c["ref"]] = fp.GetPadCount()
        mouth[c["ref"]] = fp_mouth(fp)
    return fps, extents, pincount, mouth


def marker_prim(item: "pcbnew.BOARD_ITEM", layer: str) -> dict | None:
    """An edge_mount primitive (mm) for a Dwgs.User / Cmts.User segment or text."""
    if item.GetClass() == "PCB_SHAPE" and item.GetShape() == pcbnew.SHAPE_T_SEGMENT:
        s, e = item.GetStart(), item.GetEnd()
        return {"kind": "line", "layer": layer, "x0": pcbnew.ToMM(s.x), "y0": pcbnew.ToMM(s.y),
                "x1": pcbnew.ToMM(e.x), "y1": pcbnew.ToMM(e.y)}
    if item.GetClass() != "PCB_TEXT":
        return None
    angle = (item.GetTextAngleDegrees() if hasattr(item, "GetTextAngleDegrees")
             else item.GetTextAngle().AsDegrees())
    p = item.GetPosition()
    return {"kind": "text", "layer": layer, "x": pcbnew.ToMM(p.x), "y": pcbnew.ToMM(p.y),
            "rot": float(angle), "text": str(item.GetText())}


def edge_mount_info(fp: "pcbnew.FOOTPRINT") -> em.EdgeMount:
    """edge_mount.edge_from_primitives of a footprint still at the origin, rot 0,
    front side (as load_parts leaves it): its Dwgs.User / Cmts.User segments and
    texts (the 'PCB Edge' marker), pad boxes and courtyard bbox. Raises ValueError
    on primitives edge_mount refuses."""
    layers = {getattr(pcbnew, attr): name for attr, name in MARKER_LAYERS}
    prims = [p for p in (marker_prim(it, layers[it.GetLayer()]) for it in fp.GraphicalItems()
                         if it.GetLayer() in layers) if p is not None]
    for pad in fp.Pads():
        bb = pad.GetBoundingBox()
        prims.append({"kind": "pad", "x": pcbnew.ToMM(bb.GetCenter().x),
                      "y": pcbnew.ToMM(bb.GetCenter().y), "w": pcbnew.ToMM(bb.GetWidth()),
                      "h": pcbnew.ToMM(bb.GetHeight())})
    polys = bl.courtyard_polys(fp, 0.0, 0.0, fallback=False)
    return em.edge_from_primitives(prims, bl.box_of(polys) if polys else None,
                                   name=str(fp.GetFPID().GetLibItemName()),
                                   tags=str(fp.GetKeywords()))


def edge_mounts(fps: dict) -> tuple[dict, list[str]]:
    """({ref: EdgeMount}, warnings) of every footprint, read BEFORE placement."""
    info, warnings = {}, []
    for ref, fp in fps.items():
        try:
            info[ref] = edge_mount_info(fp)
        except ValueError as e:
            info[ref] = em.NOT_EDGE_MOUNT
            warnings.append(f"{ref}: edge-mount detection failed ({e}); treated as not "
                            "edge-mount (no derived overhang)")
    return info, warnings


def place_board(board: "pcbnew.BOARD", design: dict, src: "bf.OutlineSource", sheet: tuple,
                loaded: tuple, dims: tuple[float, float]) -> tuple[dict, "bl.Layout",
                                                                   tuple, dict, list[str]]:
    """build_layout's targets, the board centred on its sheet, every footprint
    placed, "edge" connectors snapped and every courtyard checked against the
    outline (legacy: the w x h rectangle). Returns (placed, layout, (ox, oy),
    result keys, warnings)."""
    fps, extents, pincount, mouth = loaded
    edge_info, warnings = edge_mounts(fps)       # at the origin, rot 0: before placement
    layout = bl.compute_targets(design, src.outline, dims, (extents, pincount, mouth),
                                FAB_RULES_MM["m_CopperEdgeClearance"])
    ox = round((sheet[1] - layout.w) / 2.0, 3)
    oy = round((sheet[2] - layout.h) / 2.0, 3)
    placed = bl.place_footprints(board, design, fps, extents, layout, ox, oy)
    shape = src.outline or ol.from_board_block({"w": layout.w, "h": layout.h})
    notes = bl.snap_connectors(design, placed, shape, edge_info, ox, oy)
    issues, derived, found = bl.containment(design, placed, shape, edge_info, ox, oy)
    keys = {"anchors": bl.anchor_refs(design), "containment": issues,
            "fields": [[round(v, 6) + 0.0 for v in f.rect] for f in layout.fields],
            "derived_overhang": derived}
    return placed, layout, (ox, oy), keys, [*warnings, *layout.warnings, *notes, *found]


def assign_pads(design: dict, placed: dict,
                get_net: Callable[[str], object]) -> tuple[int, list[str]]:
    """Net every netlist (ref, pad): (pads assigned, missing "ref.pad (why)").
    A pad "number" can map to several copper elements (e.g. a split
    thermal/EP pad): net ALL of them, or they short on the empty net."""
    assigned, missing = 0, []
    for net_name, conns in design.get("nets", {}).items():
        ni = get_net(net_name)
        for ref, pad_num in conns:
            fp = placed.get(ref)
            if fp is None:
                missing.append(f"{ref}.{pad_num} (no footprint)")
                continue
            matched = [p for p in fp.Pads() if p.GetNumber() == str(pad_num)]
            if not matched:
                missing.append(f"{ref}.{pad_num} (no pad)")
                continue
            for p in matched:
                p.SetNet(ni)
            assigned += len(matched)
    return assigned, missing


def new_board() -> tuple["pcbnew.BOARD", list[str]]:
    """An empty 4-layer board with the JLCPCB-class design rules (so
    default-rule DRC reflects the real fab), and the warnings. A rule that
    silently fails to apply means every later DRC runs against the wrong fab
    floors, so a failed set is a hard error and a field this pcbnew version
    lacks is reported, not hidden."""
    board = pcbnew.CreateEmptyBoard()
    board.SetCopperLayerCount(4)
    skipped_rules = apply_design_rules(board)
    if not skipped_rules:
        return board, []
    return board, [f"design rules not exposed by pcbnew {pcbnew.GetBuildVersion()} and "
                   f"skipped: {', '.join(skipped_rules)}"]


def net_registry(board: "pcbnew.BOARD") -> tuple[Callable[[str], object], dict]:
    """(get_net, netmap): get_net(name) adds the board net on first use."""
    netmap: dict = {}

    def get_net(name: str) -> "pcbnew.NETINFO_ITEM":
        if name not in netmap:
            ni = pcbnew.NETINFO_ITEM(board, name)
            board.Add(ni)
            netmap[name] = ni
        return netmap[name]
    return get_net, netmap


def load_hole(footprints: str | dict, mh_fp: str) -> "pcbnew.FOOTPRINT":
    """A mounting-hole footprint, nickname-stamped like every part (D1)."""
    fp = load_footprint(footprints, mh_fp)
    return fp


def add_features(board: "pcbnew.BOARD", design: dict, placed: dict, nets: tuple,
                 frame: "bf.Frame", sheet: tuple, footprints: str | dict) -> tuple[dict, list]:
    """Edge.Cuts (+ the aux origin of an outline board), then build_features'
    preroutes, mounting holes (nickname-stamped like every part, D1), planes
    and antenna keep-out, in that order. Returns (result keys, warnings)."""
    get_net, netmap = nets
    outline_info, warnings = bf.add_board_outline(board, design, frame, sheet)
    preroutes, notes = bf.add_preroutes(board, design, placed, get_net, frame.ox, frame.oy)
    warnings = warnings + notes
    holes, notes = bf.add_mounting_holes(board, design, functools.partial(load_hole, footprints),
                                         frame, libtable.DEFAULT_MOUNTING_HOLE_FOOTPRINT)
    warnings = warnings + notes
    zones, notes = bf.add_planes(board, design, netmap, frame,
                                 FAB_RULES_MM["m_CopperEdgeClearance"])
    warnings = warnings + notes
    keepout, notes = bf.add_antenna_keepout(board, design, frame)
    return ({"zones": zones, "antenna_keepout": keepout, "mounting_holes": holes,
             "preroutes": preroutes, "outline": outline_info}, warnings + notes)


def place_silk(board: "pcbnew.BOARD") -> list[str]:
    """Silkscreen hygiene: refs placed ADJACENT to their part (visible after
    assembly), centre-on-body only as the dense fallback, everything clamped
    inside Edge.Cuts (shared with the post-route fixer). Returns warnings."""
    try:
        import silk_place
        silk = silk_place.place_refs(board)
        return [f"silkscreen: {e}" for e in silk.get("errors", [])]
    except Exception as e:
        traceback.print_exc()
        return [f"silkscreen ref placement failed ({type(e).__name__}: {e}); refs left at "
                f"their footprint defaults"]


def save_and_fill(board: "pcbnew.BOARD", out_path: str, zones: list[str], sheet: tuple,
                  legacy: bool) -> list[str]:
    """SaveBoard, the sheet splice (outline boards), then the zone fill on a
    FRESHLY LOADED board: the in-memory CreateEmptyBoard board lacks the
    connectivity the headless ZONE_FILLER needs and segfaults 9.0.4. The
    splice runs first so the fill saves the final sheet. An unfilled plane is
    a board with no ground/power copper, so a fill failure is fatal.
    Returns warnings."""
    warnings = []
    try:
        board.BuildConnectivity()
    except Exception as e:
        warnings.append(f"BuildConnectivity failed before save ({type(e).__name__}: {e}); "
                        f"ratsnest rebuilt on load")
    pcbnew.SaveBoard(out_path, board)
    if not legacy:
        bf.splice_paper(out_path, sheet)
    if zones:
        b2 = bo.load_board(out_path)
        b2.BuildConnectivity()
        pcbnew.ZONE_FILLER(b2).Fill(b2.Zones())
        pcbnew.SaveBoard(out_path, b2)
    return warnings


def main() -> None:
    netlist_path, out_path = sys.argv[1], sys.argv[2]
    footprints = footprint_libraries(sys.argv[3])
    netlist_bytes, design = read_design(netlist_path, sys.argv[6] if len(sys.argv) > 6 else None)
    # the outline source (env sidecar / board block / legacy), then KIIDs
    # seeded from the inputs so every rebuild saves the same file order
    src = bf.resolve_outline_source(design, os.environ)
    design = src.design
    pcbnew.KIID.SeedGenerator(bf.kiid_seed(netlist_bytes, src.sidecar_bytes))
    outline_wh = bf.outline_dims(src)          # None: legacy
    board, warnings = new_board()
    # The board is centred on its sheet: A4 for legacy boards (CreateEmptyBoard's
    # default, saved as (paper "A4")); an outline board gets the smallest
    # landscape size holding it + 20 mm margins, spliced in after SaveBoard
    # (page settings are an opaque SWIG object on 9.0.4 and 10.0.5).
    sheet = bf.LEGACY_SHEET if outline_wh is None else bf.choose_sheet(*outline_wh)
    warnings = warnings + bf.apply_page(board, sheet[0])
    loaded = load_parts(design, footprints)
    # ref -> "nick:name" as stamped (D1); the mounting holes are added below
    fpids = {ref: str(fp.GetFPID().GetUniStringLibId()) for ref, fp in loaded[0].items()}
    placed, layout, (ox, oy), placement, notes = place_board(board, design, src, sheet, loaded,
                                                             argv_dims(sys.argv))
    get_net, netmap = net_registry(board)
    assigned, missing_pads = assign_pads(design, placed, get_net)
    frame = bf.Frame(layout.w, layout.h, ox, oy, src.outline, src.source)
    feats, more = add_features(board, design, placed, (get_net, netmap), frame, sheet,
                               footprints)
    warnings = warnings + notes + more + place_silk(board)
    warnings = warnings + save_and_fill(board, out_path, feats["zones"], sheet,
                                        src.outline is None)
    fpids = {**fpids, **feats["mounting_holes"]}
    print(json.dumps({
        "ok": not missing_pads, "pcb": out_path, "footprints": len(placed),
        "nets": len(netmap), "pads_assigned": assigned, "missing_pads": missing_pads,
        "zones": feats["zones"], "antenna_keepout": feats["antenna_keepout"],
        "mounting_holes": list(feats["mounting_holes"]), "preroutes": feats["preroutes"],
        "board_mm": [layout.w, layout.h], "outline": feats["outline"], "fpids": fpids,
        **placement, "warnings": warnings,
    }))


if __name__ == "__main__":
    try:
        main()
    except bf.BuildError as e:          # a board the builder refuses: no trace
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e),
                          "trace": traceback.format_exc()}))
        sys.exit(1)
