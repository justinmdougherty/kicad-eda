#!/usr/bin/env python3
"""Connector edges for place_engine.functional_place (F3-08).

A connector -- a J* part that is not `interior`, or any `edge_row` part such as
an RF module whose antenna must sit at the board edge -- mates through one
board edge. This module holds that geometry: the edges' outward normals, the
quarter turn that points a connector's mouth along its edge's normal, the
per-edge orientation of a connector cluster (head on the edge, satellites
inward) and the rows / columns those clusters are packed into around the
anchor field:

              [ top row, along x    ]
    [ left  ] [ anchor field        ] [ right ]
    [ column] [ bottom row, along x ] [ column]

Board frame: mm, Y down. KiCad's rotation is clockwise-positive in that frame.
Pure math, stdlib only and Python 3.11 compatible: the KiCad-python SWIG builder
imports it through place_engine.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence

EDGES = ("top", "bottom", "left", "right")
DEFAULT_EDGE = "top"
NORMALS = {"top": (0.0, -1.0), "bottom": (0.0, 1.0),
           "left": (-1.0, 0.0), "right": (1.0, 0.0)}
QUARTER_TURNS = (0, 90, 180, 270)
MOUTHLESS = 0.4      # |mx| and |my| both below this: no real mating direction

# (bbox-centre of every member relative to the cluster's top-left, width, height)
Cluster = tuple[dict, float, float]


def rotate_cw(vec: Sequence[float], rot: int) -> tuple[float, float]:
    """`vec` turned by the quarter turn `rot` (degrees) the way KiCad turns a
    footprint, clockwise-positive in the Y-down frame:
    R(a)(x, y) = (x cos a + y sin a, -x sin a + y cos a), exact for 90-degree steps."""
    x, y = vec
    turned = {0: (x, y), 90: (y, -x), 180: (-x, -y), 270: (-y, x)}
    return turned[rot % 360]


def rot_face(mouth: Sequence[float], edge: str = DEFAULT_EDGE) -> int:
    """The quarter turn that points `mouth` (fp_mouth: the rot-0 opening
    direction) along `edge`'s outward normal; 0 for a mouthless part (a
    vertical pin header). Ties go to the first of 0/90/180/270."""
    mx, my = mouth
    if abs(mx) < MOUTHLESS and abs(my) < MOUTHLESS:
        return 0
    nx, ny = NORMALS[edge]
    dots = {}
    for rot in QUARTER_TURNS:
        rx, ry = rotate_cw((mx, my), rot)
        dots[rot] = nx * rx + ny * ry
    return max(dots, key=dots.get)


def resolve_edges(conn_edges: Mapping[str, str] | None, connectors: Sequence[str],
                  refs: Collection[str]) -> dict[str, str]:
    """{connector: edge} for every connector: DEFAULT_EDGE unless conn_edges
    names another. Entries for refs outside `refs` are ignored (one board-wide
    mapping serves the F and B sub-designs); an unknown edge name, or an entry
    for a part of this design that is not a connector, raises ValueError."""
    edges = dict(conn_edges or {})
    bad = [f"{r!r}: {e!r}" for r, e in sorted(edges.items(), key=lambda kv: str(kv[0]))
           if e not in EDGES]
    if bad:
        raise ValueError(f"conn_edges: unknown edge for {', '.join(bad)} "
                         f"(expected one of {', '.join(EDGES)})")
    cset = set(connectors)
    stray = sorted(r for r in edges if r in refs and r not in cset)
    if stray:
        raise ValueError(f"conn_edges names {', '.join(map(repr, stray))}, not an edge "
                         "connector (a J* part without 'interior', or an 'edge_row' part)")
    return {c: edges.get(c, DEFAULT_EDGE) for c in connectors}


def transposed(edge: str) -> bool:
    """True when a cluster for `edge` is laid out on transposed (h, w) sizes."""
    return edge in ("left", "right")


def orient_cluster(cluster: Cluster, edge: str) -> Cluster:
    """A cluster laid out for the top edge (head at y = 0, satellites beside and
    below it) turned to face `edge`: bottom mirrors y; left / right expect the
    cluster laid out on TRANSPOSED sizes (see transposed) and transpose it
    back, right also mirroring x -- so the head always sits on the cluster side
    that faces the edge. Mirrors and transposes keep the boxes disjoint."""
    local, cw, ch = cluster
    if edge == "top":
        return cluster
    if edge == "bottom":
        return {r: (x, ch - y) for r, (x, y) in local.items()}, cw, ch
    if edge == "left":
        return {r: (y, x) for r, (x, y) in local.items()}, ch, cw
    return {r: (ch - y, x) for r, (x, y) in local.items()}, ch, cw


def edge_groups(connectors: Sequence[str], edge_of: Mapping[str, str]) -> dict[str, list]:
    """{edge: the connectors on it, in design order} for every edge."""
    return {e: [c for c in connectors if edge_of[c] == e] for e in EDGES}


def band_extents(groups: Mapping[str, list], clusters: Mapping[str, Cluster],
                 margin: float) -> dict[str, tuple[float, float]]:
    """{edge: (length along the edge, depth into the board)}. Rows (top,
    bottom) count one margin per cluster, as the original top connector row
    did; columns (left, right) are content-true."""
    out = {}
    for e in ("top", "bottom"):
        refs = groups[e]
        out[e] = (sum(clusters[c][1] + margin for c in refs),
                  max([clusters[c][2] for c in refs], default=0.0))
    for e in ("left", "right"):
        refs = groups[e]
        out[e] = (sum(clusters[c][2] for c in refs) + margin * max(len(refs) - 1, 0),
                  max([clusters[c][1] for c in refs], default=0.0))
    return out


def anchor_row_limit(groups: Mapping[str, list], bands: Mapping[str, tuple],
                     width_limit: float, margin: float) -> float:
    """Row width for the anchor field: as wide as the top / bottom connector
    rows, or the free width (width_limit) left between the side columns.
    0 lets shelf_pack derive it from its aspect ratio."""
    side = sum(bands[e][1] + margin for e in ("left", "right") if groups[e])
    inner = width_limit - side if side else width_limit
    return max(bands["top"][0], inner, bands["bottom"][0])


def _shifted(local: Mapping[str, tuple], x0: float, y0: float) -> dict:
    return {r: (x0 + lx, y0 + ly) for r, (lx, ly) in local.items()}


def _offsets(refs: Sequence[str], clusters: Mapping[str, Cluster], margin: float,
             start: float, axis: int) -> list[tuple[str, float]]:
    """(ref, offset) packing clusters one after another along x (axis 1, by
    width) or y (axis 2, by height), one margin apart."""
    out, at = [], start
    for c in refs:
        out.append((c, at))
        at += clusters[c][axis] + margin
    return out


def place_edge_clusters(groups: Mapping[str, list], clusters: Mapping[str, Cluster],
                        bands: Mapping[str, tuple], core: tuple[float, float],
                        margin: float) -> tuple[dict, tuple[float, float, float]]:
    """Positions of every connector-cluster member around a core (the packed
    anchor field, `core` = its (w, h), (0, 0) when empty) and the core frame
    (x0, y0, core_w) the caller packs the anchors into. Top / left heads touch
    y = 0 / x = 0; right heads end at the largest x and bottom heads at the
    largest y of the whole placement (below a side column that outgrows the
    core). With only top connectors this is the original connector row."""
    top, bottom, left, right = (groups[e] for e in EDGES)
    x0 = bands["left"][1] + margin if left else 0.0
    y0 = bands["top"][1] + margin if top else 0.0
    core_w = max(bands["top"][0], core[0], bands["bottom"][0])
    pos: dict = {}
    for c, cx in _offsets(top, clusters, margin, x0, 1):
        # the original connector row, verbatim: bit-identical default placement
        pos.update({r: (cx + lx, ly) for r, (lx, ly) in clusters[c][0].items()})
    bottom_top = y0 + core[1] + margin if core[1] else y0
    bottom_end = max(bottom_top + bands["bottom"][1], bands["left"][0], bands["right"][0])
    for c, cx in _offsets(bottom, clusters, margin, x0, 1):
        pos.update(_shifted(clusters[c][0], cx, bottom_end - clusters[c][2]))
    for c, cy in _offsets(left, clusters, margin, 0.0, 2):
        pos.update(_shifted(clusters[c][0], 0.0, cy))
    x_right = x0 + core_w + margin if core_w else x0
    for c, cy in _offsets(right, clusters, margin, 0.0, 2):
        pos.update(_shifted(clusters[c][0], x_right + bands["right"][1] - clusters[c][1], cy))
    return pos, (x0, y0, core_w)
