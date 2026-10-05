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
import fp_dialect
import libtable


def mm(v):
    return pcbnew.FromMM(float(v))


def vec(x, y):
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


def apply_design_rules(board, rules_mm=FAB_RULES_MM):
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


def save_and_fill(board: "pcbnew.BOARD", out_path: str, zones: list[str], sheet: tuple,
                  legacy: bool, warnings: list[str]) -> None:
    """SaveBoard, the sheet splice (outline boards), then the zone fill on a
    FRESHLY LOADED board: the in-memory CreateEmptyBoard board lacks the
    connectivity the headless ZONE_FILLER needs and segfaults 9.0.4. The
    splice runs first so the fill saves the final sheet. An unfilled plane is
    a board with no ground/power copper, so a fill failure is fatal."""
    try:
        board.BuildConnectivity()
    except Exception as e:
        warnings.append(f"BuildConnectivity failed before save "
                        f"({type(e).__name__}: {e}); ratsnest rebuilt on load")
    pcbnew.SaveBoard(out_path, board)
    if not legacy:
        bf.splice_paper(out_path, sheet)
    if zones:
        b2 = bo.load_board(out_path)
        b2.BuildConnectivity()
        pcbnew.ZONE_FILLER(b2).Fill(b2.Zones())
        pcbnew.SaveBoard(out_path, b2)


def main() -> None:
    netlist_path, out_path = sys.argv[1], sys.argv[2]
    footprints = footprint_libraries(sys.argv[3])
    board_w, board_h = argv_dims(sys.argv)
    netlist_bytes, design = read_design(netlist_path, sys.argv[6] if len(sys.argv) > 6 else None)
    # the outline source (env sidecar / board block / legacy), then KIIDs
    # seeded from the inputs so every rebuild saves the same file order
    src = bf.resolve_outline_source(design, os.environ)
    design = src.design
    pcbnew.KIID.SeedGenerator(bf.kiid_seed(netlist_bytes, src.sidecar_bytes))
    outline_wh = bf.outline_dims(src)          # None: legacy

    board = pcbnew.CreateEmptyBoard()
    board.SetCopperLayerCount(4)
    warnings = []   # non-fatal degradations, reported in the result JSON

    # JLCPCB-class design rules (so default-rule DRC reflects the real fab).
    # A rule that silently fails to apply means every later DRC runs against
    # the wrong fab floors, so a failed set is a hard error and a field this
    # pcbnew version lacks is reported, not hidden.
    skipped_rules = apply_design_rules(board)
    if skipped_rules:
        warnings.append(f"design rules not exposed by pcbnew "
                        f"{pcbnew.GetBuildVersion()} and skipped: "
                        f"{', '.join(skipped_rules)}")

    # The board is centred on its sheet: A4 for legacy boards (CreateEmptyBoard's
    # default, saved as (paper "A4")); an outline board gets the smallest
    # landscape size holding it + 20 mm margins, spliced in after SaveBoard
    # (page settings are an opaque SWIG object on 9.0.4 and 10.0.5).
    sheet = bf.LEGACY_SHEET if outline_wh is None else bf.choose_sheet(*outline_wh)
    warnings += bf.apply_page(board, sheet[0])
    SHEET_W, SHEET_H = sheet[1], sheet[2]

    # --- load + measure every footprint (at the origin) ---
    placed = {}
    fps, extents, pincount, mouth = load_parts(design, footprints)
    # ref -> "nick:name" as stamped (D1), mounting holes added below
    fpids = {ref: str(fp.GetFPID().GetUniStringLibId()) for ref, fp in fps.items()}

    auto_rots = {}
    board_spec = design.get("board") or {}
    fixed_refs = {c["ref"] for c in design["components"] if "x" in c and "y" in c}
    auto_comps = [c for c in design["components"] if c["ref"] not in fixed_refs]
    have_full_placement = not auto_comps
    if have_full_placement:
        # caller supplied coords + board size: honor them verbatim. An
        # outline board's size is its outline bbox (it may have no w/h).
        targets = {c["ref"]: (c["x"], c["y"]) for c in design["components"]}
        if outline_wh is not None:
            board_w, board_h = outline_wh
        elif board_spec:
            board_w, board_h = float(board_spec["w"]), float(board_spec["h"])
    elif fixed_refs or outline_wh is not None:
        # MIXED placement: mechanically-spec'd parts (a HAT's header, holes,
        # connectors) sit at their exact board-relative coordinates; everything
        # else is functionally packed into the remaining field. Requires a
        # fixed board size — a spec'd part position is meaningless on an
        # auto-sized outline. Collisions surface at the G3 courtyard gate.
        # An outline board (fixed by its outline) packs its auto parts here
        # too, into fields of its bbox (F3-12 refines them with board_field).
        if not board_spec:
            print(json.dumps({"ok": False, "error":
                  'mixed placement requires "board": {"w": mm, "h": mm}'}))
            sys.exit(1)
        board_w, board_h = outline_wh or (float(board_spec["w"]), float(board_spec["h"]))
        scale = float(os.environ.get("PACK_SCALE", "1.0"))
        edge = 2.0 * scale
        has_conn = any(c["ref"][:1] == "J" and not c.get("interior")
                       for c in auto_comps)
        edge_top = 1.2 if has_conn else edge
        # The auto field is the rectangle left over after the FIXED parts (a
        # HAT's header along the top, connectors down an edge, corner holes).
        # The packer can't see those, so the netlist may carve the field
        # explicitly: auto_x/auto_w bound it horizontally, auto_y drops its
        # top below the fixed header, auto_h bounds its height. Defaults are
        # the full board inside the edge margins.
        auto_y = float(board_spec.get("auto_y", 0.0))
        y0 = edge_top + auto_y
        x0_spec = board_spec.get("auto_x")
        avail_w = float(board_spec.get("auto_w", board_w - 2 * edge))
        avail_h = float(board_spec.get("auto_h", board_h - y0 - edge))
        # Two-sided: components flagged "side":"B" pack into their own field
        # on the back of the board (the dense-HAT idiom — power stage and
        # connectors on top, support logic and passives underneath). Each side
        # is an independent functional placement over the SAME field rect;
        # F/B courtyards don't collide, so the field is effectively doubled.
        b_refs = {c["ref"] for c in auto_comps
                  if str(c.get("side", "F")).upper() == "B"}
        f_comps = [c for c in auto_comps if c["ref"] not in b_refs]
        b_comps = [c for c in auto_comps if c["ref"] in b_refs]
        # The B side may carve its OWN field (board-absolute x/y/w/h via
        # "auto_b") to dodge front-side through copper — THT connector pads
        # and thermal-via arrays exist on BOTH faces, so the back field must
        # avoid them where the front field need not.
        ab = board_spec.get("auto_b") or {}
        fields = {
            "F": (x0_spec, y0, avail_w, avail_h),
            "B": (ab.get("x", x0_spec), float(ab.get("y", y0)),
                  float(ab.get("w", avail_w)), float(ab.get("h", avail_h))),
        }
        targets = {}
        for side_comps, side_name in ((f_comps, "F"), (b_comps, "B")):
            if not side_comps:
                continue
            sx0, sy0, s_w, s_h = fields[side_name]
            sub = {**design, "components": side_comps}
            packed, rots_s, used_w, used_h = functional_place(
                sub, extents, pincount, mouth, margin=1.5 * scale,
                width_limit=s_w)
            if used_w > s_w or used_h > s_h:
                print(json.dumps({"ok": False, "error":
                      f"{side_name}-side auto cluster {used_w}x{used_h} mm does "
                      f"not fit the free field {s_w}x{s_h} mm of the "
                      f"fixed board {board_w}x{board_h} mm at pack_scale {scale}"}))
                sys.exit(1)
            x0 = (float(sx0) + (s_w - used_w) / 2.0
                  if sx0 is not None else (board_w - used_w) / 2.0)
            targets.update({ref: (x0 + bx, sy0 + by)
                            for ref, (bx, by) in packed.items()})
            auto_rots.update(rots_s)
        for c in design["components"]:
            if c["ref"] in fixed_refs:
                targets[c["ref"]] = (c["x"], c["y"])
            ov = float(c.get("overhang", 0) or 0)
            if ov and c["ref"] in targets and c["ref"] not in fixed_refs:
                tx, ty = targets[c["ref"]]
                targets[c["ref"]] = (tx, ty - ov)
    else:
        # auto: FUNCTIONAL placement — cluster each support passive at the IC it
        # serves, connectors mouth-out on the edge — then size the board to fit.
        # PACK_SCALE loosens spacing on a retry so a deterministic router that left
        # one pad unrouted gets more room.
        scale = float(os.environ.get("PACK_SCALE", "1.0"))
        packed, auto_rots, used_w, used_h = functional_place(
            design, extents, pincount, mouth, margin=1.5 * scale)
        edge = 2.0 * scale  # board margin around the placed cluster
        # Connectors must stay FLUSH to the edge they mate through, regardless
        # of pack_scale — a USB/SMA recessed 3 mm into the board can't mate
        # through an enclosure panel. The connector row sits along the top, so
        # only the top margin is pinned; the parts cluster keeps the scaled
        # breathing room on the other three sides.
        has_conn = any(c["ref"][:1] == "J" and not c.get("interior")
                       for c in design["components"])
        edge_top = 1.2 if has_conn else edge
        # corner mounting holes need a clear ring: hole at inset 4.0 with a
        # ~6.4 mm courtyard reaches 7.2 mm in from each edge
        if design.get("mounting_holes"):
            edge = max(edge, 7.5)
        board_w = round(used_w + 2 * edge, 2)
        board_h = round(used_h + edge_top + edge, 2)
        targets = {ref: (bx + edge, by + edge_top) for ref, (bx, by) in packed.items()}
        # antenna overhang: shift an edge_row part UP so its radiator section
        # extends past Edge.Cuts (the vendor-preferred RF geometry — antenna
        # over air, ground plane under the module body only)
        for c in design["components"]:
            ov = float(c.get("overhang", 0) or 0)
            if ov and c["ref"] in targets:
                tx, ty = targets[c["ref"]]
                targets[c["ref"]] = (tx, ty - ov)

    ox = round((SHEET_W - board_w) / 2.0, 3)
    oy = round((SHEET_H - board_h) / 2.0, 3)

    # --- place: anchor so each part's bbox-center lands on its target cell ---
    import math
    for c in design["components"]:
        ref = c["ref"]
        fp = fps[ref]
        rot = float(c.get("rot", auto_rots.get(ref, 0)))
        if rot:
            fp.SetOrientationDegrees(rot)
        w, h, cxo, cyo = extents[ref]
        tx, ty = targets[ref]
        # the bbox-center offset (cxo,cyo) was measured at rot 0; rotate it so the
        # bbox-center still lands on the target after the footprint is rotated.
        # NOTE: KiCad's footprint rotation is clockwise-positive (Y-down), the
        # opposite sin sign from textbook rotation — get this wrong and a 90/270
        # part's bbox lands 2x its anchor offset away. (Harmless at 0/180.)
        a = math.radians(rot)
        rx = cxo * math.cos(a) + cyo * math.sin(a)
        ry = -cxo * math.sin(a) + cyo * math.cos(a)
        ax = tx - rx + ox
        ay = ty - ry + oy
        fp.SetPosition(vec(ax, ay))
        board.Add(fp)
        # back-side part: flip around its own bbox-center so the courtyard
        # stays on the target cell, mirrored onto B.Cu. Flip needs the board
        # parent set (a parentless Flip segfaults pcbnew), hence after Add.
        if str(c.get("side", "F")).upper() == "B":
            fp.Flip(vec(tx + ox, ty + oy), True)
        placed[ref] = fp

    # --- nets + pad assignment ---
    netmap = {}

    def get_net(name: str) -> "pcbnew.NETINFO_ITEM":
        if name not in netmap:
            ni = pcbnew.NETINFO_ITEM(board, name)
            board.Add(ni)
            netmap[name] = ni
        return netmap[name]

    assigned, missing_pads = assign_pads(design, placed, get_net)

    # --- Edge.Cuts (+ aux origin), then the build_features: preroutes,
    # mounting holes, planes, antenna keep-out (each ONE function) ---
    frame = bf.Frame(board_w, board_h, ox, oy, src.outline, src.source)
    outline_info, notes = bf.add_board_outline(board, design, frame, sheet)
    warnings += notes
    preroute_count, notes = bf.add_preroutes(board, design, placed, get_net, ox, oy)
    warnings += notes
    def load_hole(mh_fp: str) -> "pcbnew.FOOTPRINT":
        """A mounting-hole footprint, nickname-stamped like every part (D1)."""
        fp = load_footprint(footprints, mh_fp)
        return fp

    holes, notes = bf.add_mounting_holes(board, design, load_hole, frame,
                                         libtable.DEFAULT_MOUNTING_HOLE_FOOTPRINT)
    warnings += notes
    fpids.update(holes)
    zones_added, notes = bf.add_planes(board, design, netmap, frame,
                                       FAB_RULES_MM["m_CopperEdgeClearance"])
    warnings += notes
    keepout_added, notes = bf.add_antenna_keepout(board, design, frame)
    warnings += notes

    # --- silkscreen hygiene: refs placed ADJACENT to their part (visible
    # after assembly), center-on-body only as the dense fallback, everything
    # clamped inside Edge.Cuts. Shared with the post-route fixer.
    try:
        import silk_place
        silk = silk_place.place_refs(board)
        warnings += [f"silkscreen: {e}" for e in silk.get("errors", [])]
    except Exception as e:
        traceback.print_exc()
        warnings.append(f"silkscreen ref placement failed ({type(e).__name__}: "
                        f"{e}); refs left at their footprint defaults")

    save_and_fill(board, out_path, zones_added, sheet, src.outline is None, warnings)

    print(json.dumps({
        "ok": not missing_pads,
        "pcb": out_path,
        "footprints": len(placed),
        "nets": len(netmap),
        "pads_assigned": assigned,
        "missing_pads": missing_pads,
        "zones": zones_added,
        "antenna_keepout": keepout_added,
        "mounting_holes": list(holes),
        "preroutes": preroute_count,
        "board_mm": [board_w, board_h],
        "outline": outline_info,
        "fpids": fpids,
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
