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
Emits a JSON result on stdout.
"""
import functools
import json
import math
import os
import sys
import traceback
from collections.abc import Callable
from pathlib import Path

import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew

import board_outline as bo
import build_features as bf
import fp_dialect
import libtable


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


from place_engine import fp_extent, fp_mouth, functional_place  # noqa: E402

DEFAULT_SIZE_MM = (50.0, 25.0)    # a fully placed legacy board with no board block

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
    rot], argv[6]) merged into NEW component dicts."""
    raw = Path(netlist_path).read_bytes()
    design = json.loads(raw.decode("utf-8"))
    if not placement_arg or placement_arg in ("", "-"):
        return raw, design
    with open(placement_arg, encoding="utf-8") as f:
        placement = json.load(f)
    comps = [{**c, **dict(zip(("x", "y", "rot"), placement[c["ref"]], strict=True))}
             if c["ref"] in placement else c for c in design["components"]]
    return raw, {**design, "components": comps}


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


def pack_scale() -> float:
    """PACK_SCALE loosens spacing on a retry, so a deterministic router that
    left one pad unrouted gets more room."""
    return float(os.environ.get("PACK_SCALE", "1.0"))


def full_targets(design: dict, board_spec: dict, outline_wh: tuple[float, float] | None,
                 dims: tuple[float, float]) -> tuple[dict, dict, float, float]:
    """Every part has x/y: honour them verbatim. The board is the outline
    bbox (an outline board may have no w/h), else the board block's w x h,
    else argv / DEFAULT_SIZE_MM. Returns (targets, rotations, w, h)."""
    targets = {c["ref"]: (c["x"], c["y"]) for c in design["components"]}
    board_w, board_h = dims
    if outline_wh is not None:
        board_w, board_h = outline_wh
    elif board_spec:
        board_w, board_h = float(board_spec["w"]), float(board_spec["h"])
    return targets, {}, board_w, board_h


def mixed_fields(board_spec: dict, auto_comps: list[dict], board_w: float, board_h: float,
                 scale: float) -> dict[str, tuple]:
    """The F and B auto fields (x0 or None, y0, w, h) of a fixed-size board:
    the rectangle left over after the FIXED parts (a HAT's header along the
    top, connectors down an edge, corner holes). The packer can't see those,
    so the netlist may carve the field: auto_x/auto_w bound it horizontally,
    auto_y drops its top below the fixed header, auto_h bounds its height
    (default: the board inside the edge margins). The B side may carve its
    OWN field (board-absolute x/y/w/h via "auto_b") to dodge front-side
    through copper: THT connector pads and thermal-via arrays exist on BOTH
    faces. Outline boards use their bbox here (F3-12 refines the fields)."""
    edge = 2.0 * scale
    has_conn = any(c["ref"][:1] == "J" and not c.get("interior") for c in auto_comps)
    edge_top = 1.2 if has_conn else edge
    y0 = edge_top + float(board_spec.get("auto_y", 0.0))
    x0_spec = board_spec.get("auto_x")
    avail_w = float(board_spec.get("auto_w", board_w - 2 * edge))
    avail_h = float(board_spec.get("auto_h", board_h - y0 - edge))
    ab = board_spec.get("auto_b") or {}
    return {"F": (x0_spec, y0, avail_w, avail_h),
            "B": (ab.get("x", x0_spec), float(ab.get("y", y0)),
                  float(ab.get("w", avail_w)), float(ab.get("h", avail_h)))}


def pack_side(design: dict, side: str, comps: list[dict], field: tuple, parts: tuple,
              board_wh: tuple[float, float], scale: float) -> tuple[dict, dict]:
    """One side's auto parts functionally placed in its field, centred
    horizontally (in auto_x/auto_w when given, else on the board):
    (targets, rotations). BuildError when the cluster does not fit."""
    extents, pincount, mouth = parts
    sx0, sy0, s_w, s_h = field
    board_w, board_h = board_wh
    packed, rots, used_w, used_h = functional_place(
        {**design, "components": comps}, extents, pincount, mouth, margin=1.5 * scale,
        width_limit=s_w)
    if used_w > s_w or used_h > s_h:
        raise bf.BuildError(f"{side}-side auto cluster {used_w}x{used_h} mm does not fit the "
                            f"free field {s_w}x{s_h} mm of the fixed board {board_w}x{board_h} "
                            f"mm at pack_scale {scale}")
    x0 = (float(sx0) + (s_w - used_w) / 2.0
          if sx0 is not None else (board_w - used_w) / 2.0)
    return {ref: (x0 + bx, sy0 + by) for ref, (bx, by) in packed.items()}, rots


def mixed_targets(design: dict, board_spec: dict, outline_wh: tuple[float, float] | None,
                  fixed_refs: set[str], parts: tuple) -> tuple[dict, dict, float, float]:
    """MIXED placement: mechanically spec'd parts (a HAT's header, holes,
    connectors) sit at their exact board-relative coordinates; everything
    else is functionally packed into the remaining field, the "side": "B"
    parts into their own field on the back (F/B courtyards don't collide,
    so the field is effectively doubled). Needs a fixed board size (a spec'd
    position is meaningless on an auto-sized board); an outline board is
    fixed by its outline. Collisions surface at the G3 courtyard gate."""
    if not board_spec:
        raise bf.BuildError('mixed placement requires "board": {"w": mm, "h": mm}')
    board_w, board_h = outline_wh or (float(board_spec["w"]), float(board_spec["h"]))
    scale = pack_scale()
    auto_comps = [c for c in design["components"] if c["ref"] not in fixed_refs]
    fields = mixed_fields(board_spec, auto_comps, board_w, board_h, scale)
    b_refs = {c["ref"] for c in auto_comps if str(c.get("side", "F")).upper() == "B"}
    targets, rots = {}, {}
    for side in ("F", "B"):
        comps = [c for c in auto_comps if (c["ref"] in b_refs) == (side == "B")]
        if comps:
            side_targets, side_rots = pack_side(design, side, comps, fields[side], parts,
                                                (board_w, board_h), scale)
            targets.update(side_targets)
            rots.update(side_rots)
    for c in design["components"]:
        if c["ref"] in fixed_refs:
            targets[c["ref"]] = (c["x"], c["y"])
        ov = float(c.get("overhang", 0) or 0)
        if ov and c["ref"] in targets and c["ref"] not in fixed_refs:
            tx, ty = targets[c["ref"]]
            targets[c["ref"]] = (tx, ty - ov)
    return targets, rots, board_w, board_h


def auto_targets(design: dict, parts: tuple) -> tuple[dict, dict, float, float]:
    """FUNCTIONAL placement (each support passive clustered at the IC it
    serves, connectors mouth-out on the edge), then the board sized to fit.
    Connectors stay FLUSH to the edge they mate through whatever the
    pack_scale (a USB/SMA recessed 3 mm can't mate through an enclosure
    panel): the connector row sits along the top, so only the top margin is
    pinned. Corner mounting holes need a clear ring (hole at inset 4.0 with a
    ~6.4 mm courtyard reaches 7.2 mm in). An "overhang" part moves UP so its
    radiator extends past Edge.Cuts (antenna over air, ground plane under
    the module body only)."""
    extents, pincount, mouth = parts
    scale = pack_scale()
    packed, rots, used_w, used_h = functional_place(design, extents, pincount, mouth,
                                                    margin=1.5 * scale)
    edge = 2.0 * scale  # board margin around the placed cluster
    has_conn = any(c["ref"][:1] == "J" and not c.get("interior") for c in design["components"])
    edge_top = 1.2 if has_conn else edge
    if design.get("mounting_holes"):
        edge = max(edge, 7.5)
    board_w = round(used_w + 2 * edge, 2)
    board_h = round(used_h + edge_top + edge, 2)
    targets = {ref: (bx + edge, by + edge_top) for ref, (bx, by) in packed.items()}
    for c in design["components"]:
        ov = float(c.get("overhang", 0) or 0)
        if ov and c["ref"] in targets:
            tx, ty = targets[c["ref"]]
            targets[c["ref"]] = (tx, ty - ov)
    return targets, rots, board_w, board_h


def placement_targets(design: dict, outline_wh: tuple[float, float] | None,
                      dims: tuple[float, float], parts: tuple) -> tuple[dict, dict, float, float]:
    """(targets: ref -> board-frame bbox centre, rotations, board w, h).
    Every part fixed: full; some fixed, or an outline board: mixed (an
    outline board packs its auto parts into fields of its bbox); else auto."""
    board_spec = design.get("board") or {}
    fixed_refs = {c["ref"] for c in design["components"] if "x" in c and "y" in c}
    if not [c for c in design["components"] if c["ref"] not in fixed_refs]:
        return full_targets(design, board_spec, outline_wh, dims)
    if fixed_refs or outline_wh is not None:
        return mixed_targets(design, board_spec, outline_wh, fixed_refs, parts)
    return auto_targets(design, parts)


def place_parts(board: "pcbnew.BOARD", design: dict, fps: dict, extents: dict, targets: dict,
                rots: dict, ox: float, oy: float) -> dict:
    """Add every footprint so its bbox centre lands on its target (sheet =
    board + (ox, oy)); returns {ref: footprint}. The bbox-centre offset was
    measured at rot 0, so it is rotated with the part. KiCad's footprint
    rotation is clockwise-positive (Y down), the opposite sin sign from the
    textbook: get it wrong and a 90/270 part lands 2x its offset away. A
    back-side part flips around its own bbox centre so its courtyard stays on
    the target, mirrored onto B.Cu; Flip needs the board parent set (a
    parentless Flip segfaults pcbnew), hence after Add."""
    placed = {}
    for c in design["components"]:
        ref = c["ref"]
        fp = fps[ref]
        rot = float(c.get("rot", rots.get(ref, 0)))
        if rot:
            fp.SetOrientationDegrees(rot)
        _w, _h, cxo, cyo = extents[ref]
        tx, ty = targets[ref]
        a = math.radians(rot)
        rx = cxo * math.cos(a) + cyo * math.sin(a)
        ry = -cxo * math.sin(a) + cyo * math.cos(a)
        fp.SetPosition(vec(tx - rx + ox, ty - ry + oy))
        board.Add(fp)
        if str(c.get("side", "F")).upper() == "B":
            fp.Flip(vec(tx + ox, ty + oy), True)
        placed[ref] = fp
    return placed


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
    fps, extents, pincount, mouth = load_parts(design, footprints)
    # ref -> "nick:name" as stamped (D1); the mounting holes are added below
    fpids = {ref: str(fp.GetFPID().GetUniStringLibId()) for ref, fp in fps.items()}
    targets, rots, board_w, board_h = placement_targets(design, outline_wh, argv_dims(sys.argv),
                                                        (extents, pincount, mouth))
    ox = round((sheet[1] - board_w) / 2.0, 3)
    oy = round((sheet[2] - board_h) / 2.0, 3)
    placed = place_parts(board, design, fps, extents, targets, rots, ox, oy)
    get_net, netmap = net_registry(board)
    assigned, missing_pads = assign_pads(design, placed, get_net)
    frame = bf.Frame(board_w, board_h, ox, oy, src.outline, src.source)
    feats, notes = add_features(board, design, placed, (get_net, netmap), frame, sheet,
                                footprints)
    warnings = warnings + notes + place_silk(board)
    warnings = warnings + save_and_fill(board, out_path, feats["zones"], sheet,
                                        src.outline is None)
    fpids = {**fpids, **feats["mounting_holes"]}
    print(json.dumps({
        "ok": not missing_pads, "pcb": out_path, "footprints": len(placed),
        "nets": len(netmap), "pads_assigned": assigned, "missing_pads": missing_pads,
        "zones": feats["zones"], "antenna_keepout": feats["antenna_keepout"],
        "mounting_holes": list(feats["mounting_holes"]), "preroutes": feats["preroutes"],
        "board_mm": [board_w, board_h], "outline": feats["outline"], "fpids": fpids,
        "warnings": warnings,
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
