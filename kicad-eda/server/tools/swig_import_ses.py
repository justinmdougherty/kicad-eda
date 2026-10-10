#!/usr/bin/env python3
"""Import a Freerouting .ses session back into the board, add GND pours on the
outer layers to unify the ground net, refill all zones, and save.

The inner GND plane (In1.Cu) can be pinched into isolated islands by the
antipads of the non-GND vias the router drops. Adding GND pours on F.Cu/B.Cu
*after* routing — so the router still used both signal layers freely — lets the
router's existing GND vias bridge those islands through the outer copper, which
reliably clears the "Zone GND <-> Zone GND" unconnected flag.

The pours follow the board outline (F3-16): the Edge.Cuts polygon inset by
POUR_INSET_MM (board_outline.plane_polyset; cutouts become holes, each piece
of a narrow neck gets a zone of its own), filled in place. A plain rectangle
keeps the legacy pour, the Edge.Cuts bounding box inset by POUR_INSET_MM, bit
for bit (that bbox includes half the edge line width, so the polygon inset
would move it by 0.05 mm); an outline that cannot be built falls back to that
rectangle with a warning.

Layer roles (D7-20a; ADR D7.6 'realising the gap', D7.8 R22 / R26): with the
optional argv[3] <stem>.stackup.json (the physical-design artifact,
kicad-eda/physical@1) the pours follow the stackup's roles instead of "GND on
F.Cu and B.Cu":
  * the ground net comes from the roles (ground_net): GND when a plane / mixed
    layer carries it or none carries a net, else the topmost plane / mixed net
    with a ground name (GND, VSS or GROUND in it: AGND, VSSA, ...); never a
    supply rail;
  * pours go only on outer signal / mixed layers lacking a pour of it (2 layers
    with B.Cu mixed GND: F.Cu only); never on a plane layer, on an 'unused' one
    (single-sided B.Cu, ADR D7.2.6) or on a mixed layer of another net;
  * a pour's local clearance is capped at the smallest solved CPWG ground_gap
    on its layer (the artifact's "lines"), so KiCad fills at the line's own
    class clearance there.
The artifact's copper must be the board's enabled copper; a mismatch or an
unreadable artifact is ok=false + "error" with the board untouched. The JSON
adds "stackup": {artifact, gnd_net, poured, pour_clearances, skipped}. Without
argv[3] nothing changes.

Output: the LAST stdout line is JSON
  {"ok": bool, "tracks": int, "vias": int, "warnings": [str], ["error": str]}
where "tracks" counts track/arc segments and "vias" counts vias. A GND-pour or
zone-fill problem is a warning (the routed copper is still saved); a missing or
unreadable board or SES and a failed SES import are ok=false + "error" and exit
1 (the board is left untouched). The legacy human line ``ses_import True
tracks N`` (N = tracks + vias, as before) is still printed BEFORE the JSON for
older callers.

Usage: <kicad-python> swig_import_ses.py <board.kicad_pcb> <routed.ses> [stackup.json]"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)

try:
    import pcbnew
except ImportError:          # the dev venv: only the pure role helpers work
    pcbnew = None

import board_outline as bo  # noqa: E402

if TYPE_CHECKING:
    import stackup_model as sm

GND_NET = "GND"
OUTER_LAYERS = ("F.Cu", "B.Cu")
POUR_INSET_MM = 0.3
USAGE_EXIT = 2
NET_ROLES = ("plane", "mixed")          # stackup_roles.NET_ROLES: layers that carry a net
POUR_ROLES = ("signal", "mixed")        # outer roles a unify pour may go on
GROUND_NAME = re.compile(r"GND|VSS|GROUND", re.IGNORECASE)


def mm(v):
    return pcbnew.FromMM(float(v))


def bbox_rect(board: pcbnew.BOARD, inset: float) -> list[tuple[float, float]]:
    """The legacy pour: the Edge.Cuts bounding box inset by `inset` mm (sheet
    mm, the expressions kept verbatim: FromMM truncates)."""
    bb = board.GetBoardEdgesBoundingBox()
    x0, y0 = pcbnew.ToMM(bb.GetLeft()), pcbnew.ToMM(bb.GetTop())
    w, h = pcbnew.ToMM(bb.GetWidth()), pcbnew.ToMM(bb.GetHeight())
    return [(x0 + inset, y0 + inset), (x0 + w - inset, y0 + inset),
            (x0 + w - inset, y0 + h - inset), (x0 + inset, y0 + h - inset)]


def _plain_rect(ps: pcbnew.SHAPE_POLY_SET) -> bool:
    """KiCad's Edge.Cuts polygon is one axis-aligned 4-corner ring, no holes."""
    if ps.OutlineCount() != 1 or ps.HoleCount(0) != 0 or ps.COutline(0).PointCount() != 4:
        return False
    ring = ps.COutline(0)
    pts = [ring.CPoint(i) for i in range(4)]
    return all(a.x == b.x or a.y == b.y for a, b in zip(pts, pts[1:] + pts[:1], strict=True))


def pour_polyset(board: pcbnew.BOARD, inset: float) -> pcbnew.SHAPE_POLY_SET | None:
    """The outline pour: Edge.Cuts inset by `inset` mm, or None for a plain
    rectangle (the legacy bbox rectangle). Raises board_outline.
    OutlineBuildError when Edge.Cuts is not one closed outline."""
    if _plain_rect(bo.polygon_outlines(board)):
        return None
    return bo.plane_polyset(board, inset)


def pieces(ps: pcbnew.SHAPE_POLY_SET) -> list[pcbnew.SHAPE_POLY_SET]:
    """One polygon set per outline of `ps` (with its holes). A zone persists
    only its first outline (SaveBoard writes Polygon(0), probed on 9.0.4), so
    every piece of a split pour needs a zone of its own."""
    out = []
    for i in range(ps.OutlineCount()):
        piece = pcbnew.SHAPE_POLY_SET()
        idx = piece.AddOutline(ps.COutline(i))
        for h in range(ps.HoleCount(i)):
            piece.AddHole(ps.CHole(i, h), idx)
        out.append(piece)
    return out


def _gnd_zone(board: pcbnew.BOARD, layer_name: str, gnd_code: int) -> tuple:
    """(an empty unfilled GND zone on `layer_name`, warnings)."""
    z = pcbnew.ZONE(board)
    z.SetLayer(board.GetLayerID(layer_name))
    z.SetNetCode(gnd_code)
    z.SetIsFilled(False)
    z.SetAssignedPriority(0)
    # solid (full) pad connection, matching the inner planes — thermal-relief
    # spokes on a signal-layer pour can starve near tight pads (starved_thermal).
    try:
        z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
    except Exception as e:  # API differs across pcbnew versions
        return z, [f"{layer_name} GND pour: full pad connection not set "
                   f"({type(e).__name__}: {e}); thermal reliefs remain"]
    return z, []


def _cap_clearance(zone: Any, clearance_mm: float) -> None:
    """The zone's local clearance capped at `clearance_mm` (the smallest CPWG
    ground_gap on its layer; never raised above the zone's own)."""
    cap, current = bo.nm(clearance_mm), zone.GetLocalClearance()
    zone.SetLocalClearance(cap if current is None else min(int(current), cap))


def add_gnd_pour(board: pcbnew.BOARD, layer_name: str, gnd_code: int,
                 inset: float = POUR_INSET_MM, clearance_mm: float | None = None) -> list[str]:
    """Add unfilled GND pours on ``layer_name`` over the board outline inset
    by ``inset`` mm (see the module doc), one zone per piece of it, each with
    its local clearance capped at ``clearance_mm`` when given. Returns a list
    of warnings (empty on a clean add)."""
    warnings = []
    try:
        polyset = pour_polyset(board, inset)
    except bo.OutlineBuildError as e:
        polyset = None
        warnings.append(f"{layer_name} GND pour: the board outline is unusable ({e}); "
                        f"poured the Edge.Cuts bounding box inset by {inset:g} mm instead")
    for piece in [None] if polyset is None else pieces(polyset):
        z, zone_warnings = _gnd_zone(board, layer_name, gnd_code)
        warnings += [w for w in zone_warnings if w not in warnings]
        if clearance_mm is not None:
            _cap_clearance(z, clearance_mm)
        if piece is None:
            o = z.Outline()
            o.NewOutline()
            for cx, cy in bbox_rect(board, inset):
                o.Append(mm(cx), mm(cy))
        else:
            bo.fill_zone_outline(z, piece)    # in place: never ZONE.SetOutline
        board.Add(z)
    return warnings


def import_ses(board, ses) -> bool:
    """Run ImportSpecctraSES with whichever signature this pcbnew exposes.
    Returns its bool result; raises RuntimeError if neither signature works."""
    last = None
    for attempt in (lambda: pcbnew.ImportSpecctraSES(board, ses),
                    lambda: pcbnew.ImportSpecctraSES(ses)):
        try:
            return bool(attempt())
        except TypeError as e:
            last = e
    raise RuntimeError(f"ImportSpecctraSES accepted neither (board, ses) nor "
                       f"(ses): {last}")


def unify_gnd(board) -> list:
    """Outer-layer GND pours so the router's GND vias bridge inner-plane
    islands. Returns warnings; never raises."""
    try:
        gnd_code = board.GetNetcodeFromNetname(GND_NET)
    except Exception as e:
        return [f"GND pour skipped: cannot look up net '{GND_NET}' "
                f"({type(e).__name__}: {e})"]
    if gnd_code <= 0:
        return [f"GND pour skipped: no '{GND_NET}' net on the board"]
    warnings = []
    try:
        existing = {(board.GetLayerName(z.GetLayer()), z.GetNetname())
                    for z in board.Zones()}
        for layer in OUTER_LAYERS:
            if (layer, GND_NET) not in existing:
                warnings += add_gnd_pour(board, layer, gnd_code)
    except Exception as e:
        warnings.append(f"GND pour failed: {type(e).__name__}: {e}")
    return warnings


# ---------------------------------------------------------------------------
# Layer roles (D7-20a): pure helpers, shared with emc_stitch.py
# ---------------------------------------------------------------------------
def ground_net(rs: sm.ResolvedStackup) -> str:
    """The ground net from the layer roles: GND when a plane / mixed layer
    carries it or none carries a net; else the topmost plane / mixed net with a
    ground name (GROUND_NAME); a supply rail never (then GND)."""
    nets = [c.net for c in rs.copper if c.role in NET_ROLES and c.net]
    if GND_NET in nets or not nets:
        return GND_NET
    return next((n for n in nets if GROUND_NAME.search(n)), GND_NET)


def _gap_mm(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def cpwg_gaps(lines: Any) -> dict[str, float]:
    """{layer: the smallest ground_gap of a solved CPWG line on it} from the
    artifact's "lines" (ADR D7.4); other line kinds and malformed entries are
    ignored."""
    out: dict[str, float] = {}
    for line in (lines.values() if isinstance(lines, Mapping) else ()):
        if not isinstance(line, Mapping) or line.get("line") != "cpwg":
            continue
        layers = line.get("layers")
        for layer, geo in (layers.items() if isinstance(layers, Mapping) else ()):
            gap = _gap_mm(geo.get("ground_gap")) if isinstance(geo, Mapping) else None
            if gap is not None:
                out = {**out, layer: min(gap, out.get(layer, gap))}
    return out


@dataclass(frozen=True)
class PourPlan:
    """The role-aware unify pours: ``gnd`` the ground net, ``layers`` the outer
    layers to pour (top first), ``clearances`` {poured layer: local clearance
    mm}, ``skipped`` "<layer>: why" for every other outer layer."""
    gnd: str
    layers: tuple[str, ...]
    clearances: Mapping[str, float]
    skipped: tuple[str, ...]

    def record(self, artifact: str) -> dict[str, Any]:
        """The result's "stackup" entry."""
        return {"artifact": artifact, "gnd_net": self.gnd, "poured": list(self.layers),
                "pour_clearances": dict(self.clearances), "skipped": list(self.skipped)}


def _skip_reason(copper: Any, gnd: str, existing: set[tuple[str, str]]) -> str | None:
    """Why the outer layer `copper` gets no unify pour, or None (it gets one)."""
    name, net = copper.name, copper.net
    if copper.role == "unused":
        return f"{name}: unused (single-sided, ADR D7.2.6): never poured"
    if copper.role not in POUR_ROLES:
        return f"{name}: a {copper.role} layer of {net}: its own pour only, no unify pour"
    if copper.role == "mixed" and net != gnd:
        return f"{name}: a mixed layer of {net}: its own pour only, no {gnd} pour"
    if (name, gnd) in existing:
        return f"{name}: already has a {gnd} pour"
    return None


def pour_plan(rs: sm.ResolvedStackup, existing: Iterable[tuple[str, str]],
              lines: Any) -> PourPlan:
    """The unify pours of the module doc for stackup `rs`, the board's copper
    zones `existing` ((layer, net) pairs) and the artifact's `lines`."""
    gnd, have, gaps = ground_net(rs), set(existing), cpwg_gaps(lines)
    outer = [c for c in rs.copper if c.name in OUTER_LAYERS]
    reasons = {c.name: _skip_reason(c, gnd, have) for c in outer}
    layers = tuple(c.name for c in outer if reasons[c.name] is None)
    return PourPlan(gnd, layers, {name: gaps[name] for name in layers if name in gaps},
                    tuple(r for r in reasons.values() if r is not None))


def read_roles(path: str | os.PathLike) -> tuple[sm.ResolvedStackup, Mapping[str, Any]]:
    """(stackup, lines) of a <stem>.stackup.json artifact: ValueError when it is
    not one (JSON, schema, stackup section), OSError when it cannot be read. The
    stackup modules load here only: the legacy path never needs them."""
    import swig_stackup as ss
    doc = ss.read_artifact(path)
    lines = doc.get("lines", {})
    if not isinstance(lines, Mapping):
        raise ValueError("the artifact's lines section is not an object")
    return ss.stackup_from_dict(doc["stackup"]), lines


def board_copper(board: Any) -> list[str]:
    """The board's enabled copper layers, top to bottom."""
    return [board.GetLayerName(lid) for lid in board.GetEnabledLayers().CuStack()]


def copper_mismatch(rs: sm.ResolvedStackup, names: Sequence[str]) -> str | None:
    """None when the artifact's copper is `names` (the board's), else why not."""
    want = list(rs.copper_names())
    if list(names) == want:
        return None
    return (f"the stackup artifact's copper ({', '.join(want)}) does not match the board's "
            f"enabled copper ({', '.join(names)}): pass this board's <stem>.stackup.json")


def existing_pours(board: Any) -> set[tuple[str, str]]:
    """(layer, net) of every copper zone, rule areas left out; layers read with
    GetLayerSet() (never ZONE.GetLayerName(): ADR D7.1)."""
    return {(board.GetLayerName(lid), z.GetNetname()) for z in board.Zones()
            if not z.GetIsRuleArea() for lid in z.GetLayerSet().CuStack()}


def net_code(board: Any, net: str) -> int:
    """The board's net code of `net`, 0 when the board has no such net (pcbnew
    9.0.4 raises IndexError for an unknown name instead of returning -1)."""
    try:
        return max(int(board.GetNetcodeFromNetname(net)), 0)
    except (IndexError, KeyError):
        return 0


def unify_by_roles(board: Any, rs: sm.ResolvedStackup,
                   lines: Any) -> tuple[list[str], PourPlan | None]:
    """The role-aware unify pours (module doc). Returns (warnings, the plan as
    carried out: no layers when the ground net is missing); never raises."""
    try:
        plan = pour_plan(rs, existing_pours(board), lines)
        gnd_code = net_code(board, plan.gnd)
    except Exception as e:
        return [f"GND pour failed: {type(e).__name__}: {e}"], None
    if gnd_code <= 0:
        return ([f"GND pour skipped: no '{plan.gnd}' net on the board"],
                replace(plan, layers=(), clearances={}))
    warnings: list[str] = []
    try:
        for layer in plan.layers:
            warnings += add_gnd_pour(board, layer, gnd_code,
                                     clearance_mm=plan.clearances.get(layer))
    except Exception as e:
        warnings.append(f"GND pour failed: {type(e).__name__}: {e}")
    return warnings, plan


def refill_zones(board) -> list:
    """Rebuild connectivity and fill every zone. Returns warnings."""
    try:
        board.BuildConnectivity()
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    except Exception as e:
        return [f"zone fill failed: {type(e).__name__}: {e} — zones saved "
                f"unfilled; refill in KiCad (B) or re-run DRC to verify"]
    return []


def count_copper(board):
    """(track/arc segments, vias) on the board."""
    vias = sum(1 for t in board.GetTracks() if t.GetClass() == "PCB_VIA")
    return len(board.GetTracks()) - vias, vias


def _fail(error: str) -> int:
    print(json.dumps({"ok": False, "tracks": 0, "vias": 0, "warnings": [], "error": error}))
    return 1


def _prepare(pcb: str, ses: str, artifact: str | None) -> tuple[Any, tuple | None] | str:
    """(the board, (stackup, lines) or None), or the error: the SES, the
    artifact and the board are checked before anything is imported."""
    # pcbnew reports a missing/unparseable SES through a GUI error dialog,
    # which blocks forever headless — check what we can before calling it.
    if not os.path.isfile(ses):
        return f"SES file not found: {ses}"
    roles = None
    if artifact:
        try:
            roles = read_roles(artifact)
        except (OSError, ValueError) as e:
            return f"stackup artifact {artifact} is unusable: {e}"
    try:
        board = bo.load_board(pcb)        # missing: refused before pcbnew; None: an error
    except (FileNotFoundError, RuntimeError) as e:
        return f"cannot load the board: {e}"
    bad = copper_mismatch(roles[0], board_copper(board)) if roles else None
    return bad if bad else (board, roles)


def _unify(board: Any, roles: tuple | None, artifact: str | None) -> tuple[list, dict]:
    """(warnings, extra result keys): the legacy pours, or the role-aware ones."""
    if roles is None:
        return unify_gnd(board), {}
    warnings, plan = unify_by_roles(board, *roles)
    return warnings, {"stackup": plan.record(artifact) if plan else {"artifact": artifact}}


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return USAGE_EXIT
    pcb, ses = sys.argv[1], sys.argv[2]
    artifact = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else None
    prepared = _prepare(pcb, ses, artifact)
    if isinstance(prepared, str):
        return _fail(prepared)
    board, roles = prepared
    try:
        ok = import_ses(board, ses)
    except Exception as e:
        return _fail(f"SES import raised {type(e).__name__}: {e}")
    if not ok:
        return _fail(f"ImportSpecctraSES returned False for {ses}")
    warnings, extra = _unify(board, roles, artifact)
    warnings += refill_zones(board)
    pcbnew.SaveBoard(pcb, board)
    tracks, vias = count_copper(board)
    print("ses_import", ok, "tracks", tracks + vias)   # legacy line, same as before
    print(json.dumps({"ok": True, "tracks": tracks, "vias": vias,
                      "warnings": warnings, **extra}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
