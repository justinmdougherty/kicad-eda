"""DXF fixture builders for tests/test_dxf_io.py and tests/test_dxf_loops.py.

Every DXF is generated with ezdxf. The Fusion-like fixtures copy what real
Fusion 360 sketch exports contain (surveyed from public exports): R2013,
``$INSUNITS = 5`` (cm), a ``<design> vN_SketchK`` layer carrying the
``NFusion_SketchTransform`` XDATA, POINT entities for sketch points, sketches
on the XZ plane written in world coordinates with ARC extrusion (0, 1, 0),
and geometry wrapped in a block INSERT.
"""
from __future__ import annotations

import math
from pathlib import Path

import ezdxf
from ezdxf.math import OCS

T90 = math.tan(math.pi / 8)          # bulge of a 90-degree arc
IDENTITY = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)
# maimai_touch v8.dxf (a real Fusion export): a sketch on the XZ plane
XZ_SKETCH = (1, 0, 0, 0, 0, 0, 1, 0, 0, -1, 0, 0, 0, 0, 0, 1)
FUSION_APPID = "NFusion_SketchTransform"
SKETCH_LAYER = "board v3_Sketch1"

# the probe file from the research spike: an R12 ENTITIES-only DXF, no HEADER
R12_NO_HEADER = (
    "0\nSECTION\n2\nENTITIES\n"
    "0\nLINE\n8\n0\n10\n0\n20\n0\n30\n0\n11\n50\n21\n0\n31\n0\n"
    "0\nLINE\n8\n0\n10\n50\n20\n0\n30\n0\n11\n50\n21\n30\n31\n0\n"
    "0\nLINE\n8\n0\n10\n50\n20\n30\n30\n0\n11\n0\n21\n30\n31\n0\n"
    "0\nLINE\n8\n0\n10\n0\n20\n30\n30\n0\n11\n0\n21\n0\n31\n0\n"
    "0\nCIRCLE\n8\n0\n10\n5\n20\n5\n30\n0\n40\n1.6\n"
    "0\nENDSEC\n0\nEOF\n")

EXPECTED_SLOT = [[40, 18], [30, 18, -1], [30, 22], [40, 22, -1]]


def new_doc(insunits: int = 4) -> tuple:
    doc = ezdxf.new("R2013")
    doc.header["$INSUNITS"] = insunits
    return doc, doc.modelspace()


def fusion_doc(insunits: int = 5, transform: tuple | None = IDENTITY,
               layer: str = SKETCH_LAYER) -> tuple:
    """A document shaped like a Fusion 360 sketch export (cm, sketch layer)."""
    doc, msp = new_doc(insunits)
    doc.header["$MEASUREMENT"] = 0         # Fusion writes 0 even for metric designs
    add_sketch_layer(doc, layer, transform)
    return doc, msp


def add_sketch_layer(doc, layer: str, transform: tuple | None,
                     raw: str | None = None) -> None:
    """A layer with Fusion's sketch transform XDATA (``raw`` overrides the text)."""
    lay = doc.layers.add(layer)
    if transform is not None or raw is not None:
        if not doc.appids.has_entry(FUSION_APPID):
            doc.appids.add(FUSION_APPID)
        text = raw if raw is not None else ";".join(f"{v:.6f}" for v in transform)
        lay.set_xdata(FUSION_APPID, [(1000, text)])


def save(doc, tmp_path: Path, name: str = "board.dxf") -> Path:
    path = tmp_path / name
    doc.saveas(path)
    return path


class Sketch:
    """Adds sketch-frame (Y up) geometry to a modelspace, scaled by 1/k (k=10
    writes cm), optionally lifted onto the Fusion XZ plane (world = M . s)."""

    def __init__(self, msp, layer: str = SKETCH_LAYER, k: float = 1.0,
                 xz: bool = False) -> None:
        self.msp, self.layer, self.k, self.xz = msp, layer, k, xz

    def _w(self, x: float, y: float) -> tuple:
        x, y = x / self.k, y / self.k
        return (x, 0.0, -y) if self.xz else (x, y, 0.0)

    def _attrs(self) -> dict:
        attrs = {"layer": self.layer}
        if self.xz:
            attrs["extrusion"] = (0, 1, 0)
        return attrs

    def _ocs(self, x: float, y: float) -> tuple:
        if not self.xz:
            return (x / self.k, y / self.k)
        return tuple(OCS((0, 1, 0)).from_wcs(self._w(x, y)))

    def line(self, a: tuple, b: tuple) -> None:
        self.msp.add_line(self._w(*a), self._w(*b), dxfattribs={"layer": self.layer})

    def arc(self, c: tuple, r: float, a0: float, a1: float) -> None:
        shift = 180.0 if self.xz else 0.0   # OCS(+Y) is the sketch rotated 180 deg
        self.msp.add_arc(self._ocs(*c), r / self.k, a0 + shift, a1 + shift,
                         dxfattribs=self._attrs())

    def circle(self, c: tuple, r: float) -> None:
        self.msp.add_circle(self._ocs(*c), r / self.k, dxfattribs=self._attrs())

    def rounded_rect(self, x0: float, y0: float, x1: float, y1: float, r: float) -> None:
        self.line((x0 + r, y0), (x1 - r, y0))
        self.arc((x1 - r, y0 + r), r, 270, 360)
        self.line((x1, y0 + r), (x1, y1 - r))
        self.arc((x1 - r, y1 - r), r, 0, 90)
        self.line((x1 - r, y1), (x0 + r, y1))
        self.arc((x0 + r, y1 - r), r, 90, 180)
        self.line((x0, y1 - r), (x0, y0 + r))
        self.arc((x0 + r, y0 + r), r, 180, 270)

    def slot(self, x0: float, x1: float, y: float, r: float) -> None:
        self.line((x0, y - r), (x1, y - r))
        self.arc((x1, y), r, 270, 90)
        self.line((x1, y + r), (x0, y + r))
        self.arc((x0, y), r, 90, 270)


def fusion_board(sk: Sketch) -> None:
    """60 x 40 mm, r=3 corners, source origin inside the board, a 1.6 mm
    hole on the origin and a 10 x 4 mm slot."""
    sk.rounded_rect(-10, -30, 50, 10, 3)
    sk.circle((0, 0), 1.6)
    sk.slot(20, 30, -10, 2)


def rect_poly(msp, x0: float, y0: float, x1: float, y1: float, layer: str = "0"):
    return msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True,
                              dxfattribs={"layer": layer})


def rect_lines(msp, gap: float = 0.0) -> list:
    """Four LINEs of a 50 x 30 rectangle; the top one stops `gap` short."""
    return [msp.add_line((0, 0), (50, 0)), msp.add_line((50, 0), (50, 30)),
            msp.add_line((50, 30), (gap, 30)), msp.add_line((0, 30), (0, 0))]


def board_rounded_rect(w: float, h: float, r: float) -> list:
    """The expected board-frame loop (Y down, positive orientation)."""
    return [[r, 0], [w - r, 0, T90], [w, r], [w, h - r, T90], [w - r, h],
            [r, h, T90], [0, h - r], [0, r, T90]]


def canon(loop: list) -> list:
    """Vertices rounded and rotated to start at the smallest (x, y)."""
    pts = [(round(v[0], 4) + 0.0, round(v[1], 4) + 0.0,
            round(v[2] if len(v) > 2 else 0.0, 6) + 0.0) for v in loop]
    k = min(range(len(pts)), key=lambda i: (pts[i][0], pts[i][1]))
    return pts[k:] + pts[:k]


def cutout_loops(block: dict) -> list:
    return sorted((canon(c["loop"]) for c in block.get("cutouts", []) if "loop" in c))


def cutout_circles(block: dict) -> list:
    return sorted([round(v, 4) for v in c["circle"]]
                  for c in block.get("cutouts", []) if "circle" in c)


def _seg_dist(p: tuple, a: tuple, b: tuple) -> float:
    ax, ay = b[0] - a[0], b[1] - a[1]
    L2 = ax * ax + ay * ay
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * ax + (p[1] - a[1]) * ay) / L2))
    return math.hypot(p[0] - a[0] - t * ax, p[1] - a[1] - t * ay)


def ring_dist(p: tuple, ring: list) -> float:
    """Distance from p to a closed polyline (straight edges)."""
    return min(_seg_dist(p, ring[i], ring[(i + 1) % len(ring)]) for i in range(len(ring)))
