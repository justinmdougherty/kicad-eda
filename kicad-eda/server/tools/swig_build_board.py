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

Attributes (D4, F46-19): board_only parts are loaded, placed and netted like
any other part; every component's board_only / dnp / in_bom flags (booleans,
else the build is refused) become footprint attributes through
swig_anchor.apply_attrs (board-only, DNP, excluded from the BOM / position
files; flags only turn attributes on). The result adds "anchored" (how many
parts "at" placed), "board_only" (refs) and "attr_warnings" (setters this
pcbnew lacks; also warnings), so a keep-mode rebuild round-trips "at" and
the attributes through the saved board.

Stackup (D7-14, ADR D7.5): swig_stackup.load_or_resolve gives the stackup
(the netlist's; cross-checked with the env KICAD_EDA_STACKUP artifact when
set, a stale one is a warning). apply_layers sets the copper count, each
layer's type from its role and the board thickness; one pour per plane /
mixed layer on its net (an outer mixed pour's local clearance = the smallest
CPWG ground_gap on it: the artifact's solved lines, else net_specs with an
explicit ground_gap); no pour on a signal or unused layer (single-sided B.Cu);
the legacy antenna_keepout is a rule area on every enabled copper layer
(rule_areas). "zones" stays ["<layer>:<net>"] of the pours, the layer read
with board.GetLayerName(zone.GetLayer()). After SaveBoard, swig_stackup.finalize
ALWAYS splices the stackup block (not for the legacy shorthand), reloads,
fills, saves and verifies; a mismatch (copper, types, thickness, block, zone
roles, a missing pour or keep-out) is ok false with "error". The result adds
"stackup" (source, preset, thickness, copper, spliced, pour_clearances,
problems), "layer_roles" ([{layer, role, net, type}]) and "rule_areas"
([{name, layers}] read back).
"""
import functools
import json
import math
import os
import sys
import traceback
from collections.abc import Callable, Mapping
from pathlib import Path

import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew

import build_features as bf
import build_layout as bl
import edge_mount as em
import fp_dialect
import libtable
import outline as ol
import stackup_model as sm
import swig_anchor as sa
import swig_stackup as ss


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


def apply_attributes(design: dict, placed: dict) -> list[str]:
    """swig_anchor.apply_attrs on every component's footprint (D4 board_only,
    dnp, in_bom; a flag only turns attributes ON, a library's own stay).
    Returns its warnings, one per setter this pcbnew lacks (never faked); the
    flags are booleans (build_layout.check_design refused anything else)."""
    return [w for c in design["components"] for w in sa.apply_attrs(placed[c["ref"]], c, pcbnew)]


def place_board(board: "pcbnew.BOARD", design: dict, src: "bf.OutlineSource", sheet: tuple,
                loaded: tuple, dims: tuple[float, float]) -> tuple[dict, "bl.Layout",
                                                                   tuple, dict, list[str]]:
    """build_layout's targets, the board centred on its sheet, every footprint
    placed with its D4 attributes, "edge" connectors snapped and every
    courtyard checked against the outline (legacy: the w x h rectangle).
    Returns (placed, layout, (ox, oy), result keys, warnings)."""
    fps, extents, pincount, mouth = loaded
    edge_info, warnings = edge_mounts(fps)       # at the origin, rot 0: before placement
    layout = bl.compute_targets(design, src.outline, dims, (extents, pincount, mouth),
                                FAB_RULES_MM["m_CopperEdgeClearance"])
    ox = round((sheet[1] - layout.w) / 2.0, 3)
    oy = round((sheet[2] - layout.h) / 2.0, 3)
    placed = bl.place_footprints(board, design, fps, extents, layout, ox, oy)
    attr_warnings = apply_attributes(design, placed)
    shape = src.outline or ol.from_board_block({"w": layout.w, "h": layout.h})
    notes = bl.snap_connectors(design, placed, shape, edge_info, ox, oy)
    issues, derived, found = bl.containment(design, placed, shape, edge_info, ox, oy)
    anchors = bl.anchor_refs(design)
    keys = {"anchors": anchors, "containment": issues,
            "fields": [[round(v, 6) + 0.0 for v in f.rect] for f in layout.fields],
            "derived_overhang": derived, "anchored": len(anchors),
            "board_only": [c["ref"] for c in design["components"] if c.get("board_only") is True],
            "attr_warnings": attr_warnings}
    return placed, layout, (ox, oy), keys, [*warnings, *layout.warnings, *attr_warnings,
                                            *notes, *found]


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


def load_stackup(design: dict) -> "ss.LoadedStackup":
    """The stackup to build (swig_stackup.load_or_resolve: the netlist's,
    cross-checked with $KICAD_EDA_STACKUP when set). BuildError when the
    netlist stackup does not resolve (an ST-E error; G1 lint reports it)."""
    try:
        return ss.load_or_resolve(design)
    except ValueError as e:
        raise bf.BuildError(f"stackup: {e}") from e


def new_board(rs: sm.ResolvedStackup) -> tuple["pcbnew.BOARD", list[str]]:
    """An empty board with the stackup's copper layers, layer types and
    thickness (swig_stackup.apply_layers, ADR D7.5 step 1) and the
    JLCPCB-class design rules (so default-rule DRC reflects the real fab),
    and the warnings. A rule that silently fails to apply means every later
    DRC runs against the wrong fab floors, so a failed set is a hard error and
    a field this pcbnew version lacks is reported, not hidden."""
    board = pcbnew.CreateEmptyBoard()
    ss.apply_layers(board, rs)
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


OUTER = ("F.Cu", "B.Cu")


def _positive_mm(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0


def _artifact_gaps(lines: Mapping) -> list[tuple[str, float]]:
    """(layer, ground_gap) of every solved CPWG line of the artifact (ADR D7.4)."""
    out = []
    for line in lines.values():
        if not isinstance(line, Mapping) or line.get("line") != "cpwg":
            continue
        layers = line.get("layers")
        for layer, geo in (layers.items() if isinstance(layers, Mapping) else ()):
            gap = geo.get("ground_gap") if isinstance(geo, Mapping) else None
            out += [(layer, float(gap))] if _positive_mm(gap) else []
    return out


def _declared_gaps(design: Mapping) -> list[tuple[str, float]]:
    """(layer, ground_gap) of net_specs with an explicit ground_gap on a CPWG
    line ("cpwg", or "auto" with freq_hz on an outer layer: ADR D7.7) on its
    "layers" (default F.Cu, the primary outer layer)."""
    specs, out = design.get("net_specs"), []
    for spec in (specs.values() if isinstance(specs, Mapping) else ()):
        if not isinstance(spec, Mapping) or not _positive_mm(spec.get("ground_gap")):
            continue
        line = spec.get("line", "auto")
        if line == "cpwg" or (line == "auto" and _positive_mm(spec.get("freq_hz"))):
            layers = spec.get("layers") if isinstance(spec.get("layers"), list) else ["F.Cu"]
            out += [(layer, float(spec["ground_gap"])) for layer in layers
                    if isinstance(layer, str)]
    return out


def pour_clearances(design: Mapping, rs: sm.ResolvedStackup,
                    lines: Mapping) -> dict[str, float]:
    """{outer mixed layer: the smallest CPWG ground_gap on it (mm)}: the local
    clearance of that layer's pour, so KiCad fills at the line's own class
    clearance = ground_gap (ADR D7.6 'realising the gap'). ``lines`` is the
    fresh artifact's solved lines (artifact_lines), plus net_specs gaps."""
    mixed = {c.name for c in rs.copper if c.role == "mixed" and c.name in OUTER}
    out: dict[str, float] = {}
    for layer, gap in _artifact_gaps(lines) + _declared_gaps(design):
        if layer in mixed:
            out = {**out, layer: min(gap, out.get(layer, gap))}
    return out


def artifact_lines(loaded: "ss.LoadedStackup") -> Mapping:
    """The artifact's "lines" when the stackup came from a fresh artifact, else {}."""
    if loaded.source != "artifact" or not loaded.artifact:
        return {}
    try:
        lines = ss.read_artifact(loaded.artifact).get("lines")
    except (OSError, ValueError):
        return {}
    return lines if isinstance(lines, Mapping) else {}


def add_features(board: "pcbnew.BOARD", design: dict, placed: dict, nets: tuple,
                 frame: "bf.Frame", sheet: tuple, footprints: str | dict,
                 plan: tuple[dict, dict]) -> tuple[dict, list]:
    """Edge.Cuts (+ the aux origin of an outline board), then build_features'
    preroutes, mounting holes (nickname-stamped like every part, D1), planes
    (``plan`` = ({layer: net} of the plane / mixed layers, {layer: local
    clearance mm})) and antenna keep-out, in that order. Returns (result
    keys, warnings)."""
    get_net, netmap = nets
    outline_info, warnings = bf.add_board_outline(board, design, frame, sheet)
    preroutes, notes = bf.add_preroutes(board, design, placed, get_net, frame.ox, frame.oy)
    warnings = warnings + notes
    holes, notes = bf.add_mounting_holes(board, design, functools.partial(load_hole, footprints),
                                         frame, libtable.DEFAULT_MOUNTING_HOLE_FOOTPRINT)
    warnings = warnings + notes
    zones, notes = bf.add_planes(board, plan[0], netmap, frame,
                                 FAB_RULES_MM["m_CopperEdgeClearance"], plan[1])
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


def save_and_finalize(board: "pcbnew.BOARD", out_path: str, rs: sm.ResolvedStackup,
                      sheet: tuple, legacy: bool) -> tuple[dict, list[str]]:
    """SaveBoard, the sheet splice (outline boards), then ALWAYS
    swig_stackup.finalize (ADR D7.5 steps 3-5): the stackup block spliced in
    (not for the legacy shorthand), a FRESH LoadBoard (the in-memory
    CreateEmptyBoard board lacks the connectivity the headless ZONE_FILLER
    needs and segfaults 9.0.4; the load also reads <stem>.kicad_dru), the
    zone fill, SaveBoard and the copper / types / thickness / block / zone-role
    check (the pour coverage is verify()'s). The paper splice runs first so
    the fill saves the final sheet. Returns (finalize's result, warnings)."""
    warnings = []
    try:
        board.BuildConnectivity()
    except Exception as e:
        warnings.append(f"BuildConnectivity failed before save ({type(e).__name__}: {e}); "
                        f"ratsnest rebuilt on load")
    pcbnew.SaveBoard(out_path, board)
    if not legacy:
        bf.splice_paper(out_path, sheet)
    return ss.finalize(out_path, rs, fill=True, require_planes=False), warnings


def verify(fin: Mapping, zones: list[str], keepout: Mapping | None,
           rs: sm.ResolvedStackup) -> list[str]:
    """finalize's problems, plus every pour the builder made present on the
    saved board and the antenna keep-out on every enabled copper layer."""
    saved = {f"{z['layer']}:{z['net']}" for z in fin["zones"] if not z["rule_area"]}
    problems = [*fin["problems"], *(f"the {z} pour is missing from the saved board"
                                    for z in zones if z not in saved)]
    if keepout is None:
        return problems
    name, copper = keepout.get("name", "ANTENNA"), list(rs.copper_names())
    areas = [z["layers"] for z in fin["zones"] if z["rule_area"] and z["name"] == name]
    if copper not in areas:
        problems.append(f"the antenna keep-out {name!r} covers {areas or 'no layer'} on the "
                        f"saved board, not every copper layer {copper}")
    return problems


def stackup_record(loaded: "ss.LoadedStackup", fin: Mapping, clearances: Mapping,
                   problems: list[str]) -> dict:
    """The result's "stackup": what was built and how it verified."""
    rs = loaded.stackup
    return {"source": rs.source, "from": loaded.source, "artifact": loaded.artifact,
            "preset": rs.preset, "fab": rs.fab, "copper_layers": rs.copper_layers,
            "kicad_copper_count": rs.kicad_copper_count,
            "thickness_mm": rs.board_thickness_computed, "saved_thickness_mm": fin["thickness_mm"],
            "copper": fin["copper"], "spliced": fin["spliced"], "filled": fin["filled"],
            "pour_clearances": dict(clearances), "problems": problems}


def layer_roles(rs: sm.ResolvedStackup) -> list[dict]:
    """[{layer, role, net, type}] top to bottom (type = the KiCad layer type)."""
    return [{"layer": c.name, "role": c.role, "net": c.net, "type": c.kicad_type}
            for c in rs.copper]


def main() -> None:
    netlist_path, out_path = sys.argv[1], sys.argv[2]
    footprints = footprint_libraries(sys.argv[3])
    netlist_bytes, design = read_design(netlist_path, sys.argv[6] if len(sys.argv) > 6 else None)
    # the outline source (env sidecar / board block / legacy), then KIIDs
    # seeded from the inputs so every rebuild saves the same file order
    src = bf.resolve_outline_source(design, os.environ)
    design = src.design
    pcbnew.KIID.SeedGenerator(bf.kiid_seed(netlist_bytes, src.sidecar_bytes))
    stack = load_stackup(design)
    outline_wh = bf.outline_dims(src)          # None: legacy
    board, warnings = new_board(stack.stackup)
    warnings = warnings + ([stack.note] if stack.note else [])
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
    clearances = pour_clearances(design, stack.stackup, artifact_lines(stack))
    feats, more = add_features(board, design, placed, (get_net, netmap), frame, sheet,
                               footprints, (stack.stackup.planes(), clearances))
    warnings = warnings + notes + more + place_silk(board)
    fin, more = save_and_finalize(board, out_path, stack.stackup, sheet, src.outline is None)
    problems = verify(fin, feats["zones"], feats["antenna_keepout"], stack.stackup)
    fpids = {**fpids, **feats["mounting_holes"]}
    print(json.dumps({
        "ok": not missing_pads and not problems, "pcb": out_path, "footprints": len(placed),
        "nets": len(netmap), "pads_assigned": assigned, "missing_pads": missing_pads,
        "zones": feats["zones"], "antenna_keepout": feats["antenna_keepout"],
        "rule_areas": [{"name": z["name"], "layers": z["layers"]} for z in fin["zones"]
                       if z["rule_area"]],
        "stackup": stackup_record(stack, fin, clearances, problems),
        "layer_roles": layer_roles(stack.stackup),
        "mounting_holes": list(feats["mounting_holes"]), "preroutes": feats["preroutes"],
        "board_mm": [layout.w, layout.h], "outline": feats["outline"], "fpids": fpids,
        **placement, "warnings": warnings + more,
        **({"error": "stackup verification failed: " + "; ".join(problems)} if problems else {}),
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
