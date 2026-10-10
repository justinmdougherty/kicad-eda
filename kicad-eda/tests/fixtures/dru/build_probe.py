"""KiCad-Python builder of the DRU probe board for tests/test_dru_writer_live.py (D7-12a).

usage: <KiCad python> build_probe.py <probe.json> <out.kicad_pcb> <server/tools>

Draws probe.json's "board": a copper_layers board with an Edge.Cuts rectangle of
`size` at `origin`, every net, each zone (outline = the board inset by `inset`,
filled in place through Outline(), never ZONE.SetOutline) and each straight track
(`from` / `to` relative to `origin`, mm). Then SaveBoard, a FRESH LoadBoard (the
in-memory CreateEmptyBoard board lacks the connectivity the headless ZONE_FILLER
needs and crashes 9.0.4), BuildConnectivity, the zone fill and SaveBoard again.
Write the probe in an empty directory: LoadBoard reads <stem>.kicad_dru when present.
Prints one JSON object line: {board, kicad, layers, tracks, zones, filled}.
stdlib + pcbnew only; kicad_quiet is imported before pcbnew (no modal wx dialogs).
"""
import json
import sys

sys.path.insert(0, sys.argv[3])
import kicad_quiet  # noqa: E402,F401  (before pcbnew: no modal wx dialogs)
import pcbnew  # noqa: E402

EDGE_WIDTH_MM = 0.1


def nm(value_mm):
    return int(round(float(value_mm) * 1_000_000))


def point(origin, xy):
    return pcbnew.VECTOR2I(nm(origin[0] + xy[0]), nm(origin[1] + xy[1]))


def layer_id(board, name):
    lid = board.GetLayerID(name)
    if lid < 0 or not board.IsLayerEnabled(lid):
        raise SystemExit(f"layer {name} is not enabled on the probe board")
    return lid


def add_outline(board, origin, size):
    w, h = size
    corners = [(0, 0), (w, 0), (w, h), (0, h)]
    for a, b in zip(corners, corners[1:] + corners[:1], strict=True):
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetStart(point(origin, a))
        seg.SetEnd(point(origin, b))
        seg.SetWidth(nm(EDGE_WIDTH_MM))
        board.Add(seg)


def add_zone(board, spec, nets, origin, size):
    zone = pcbnew.ZONE(board)
    zone.SetLayer(layer_id(board, spec["layer"]))
    zone.SetNetCode(nets[spec["net"]].GetNetCode())
    zone.SetIsFilled(False)
    zone.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
    inset, (w, h) = spec["inset"], size
    outline = zone.Outline()
    outline.RemoveAllContours()
    outline.NewOutline()
    for xy in ((inset, inset), (w - inset, inset), (w - inset, h - inset), (inset, h - inset)):
        p = point(origin, xy)
        outline.Append(p.x, p.y)
    board.Add(zone)


def add_track(board, spec, nets, origin):
    track = pcbnew.PCB_TRACK(board)
    track.SetLayer(layer_id(board, spec["layer"]))
    track.SetStart(point(origin, spec["from"]))
    track.SetEnd(point(origin, spec["to"]))
    track.SetWidth(nm(spec["width"]))
    track.SetNet(nets[spec["net"]])
    board.Add(track)


def draw(spec):
    board = pcbnew.CreateEmptyBoard()
    board.SetCopperLayerCount(int(spec["copper_layers"]))
    nets = {}
    for name in spec["nets"]:
        nets[name] = pcbnew.NETINFO_ITEM(board, name)
        board.Add(nets[name])
    origin, size = spec["origin"], spec["size"]
    add_outline(board, origin, size)
    for zone in spec["zones"]:
        add_zone(board, zone, nets, origin, size)
    for track in spec["tracks"]:
        add_track(board, track, nets, origin)
    return board


def main():
    with open(sys.argv[1], encoding="utf-8") as fh:
        spec = json.load(fh)["board"]
    out = sys.argv[2]
    if pcbnew.SaveBoard(out, draw(spec)) is False:
        raise SystemExit(f"SaveBoard failed for {out}")
    board = pcbnew.LoadBoard(out)
    if board is None:
        raise SystemExit(f"LoadBoard returned None for {out}")
    board.BuildConnectivity()
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    if pcbnew.SaveBoard(out, board) is False:
        raise SystemExit(f"SaveBoard failed for {out}")
    zones = list(board.Zones())
    print(json.dumps({"board": out, "kicad": pcbnew.Version(),
                      "layers": board.GetCopperLayerCount(),
                      "tracks": len(list(board.GetTracks())), "zones": len(zones),
                      "filled": all(z.IsFilled() for z in zones)}))


if __name__ == "__main__":
    main()
