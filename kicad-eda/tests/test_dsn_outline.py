"""server/tools/dsn_outline.py: the Freerouting outline rewrite of KiCad's DSN export (F3-02).

Unit tier (no KiCad): min_class_clearance (untyped rule clearances of the
structure, every class and net, in the file's unit; a typed smd_smd clearance
does not count), and rewrite_outline on

* tests/fixtures/dsn/poly_outline_k9.dsn.txt, KiCad 9.0.4's export of the
  wave-1 T10 board (rounded 60 x 40, r = 4, a stadium slot and a 5 mm hole;
  .txt because .gitignore ignores *.dsn), read as LF and as CRLF: the boundary
  shrinks by delta = 0.3 - 0.2 + 0.02 = 0.12 mm per side, the circle keepout
  becomes a 2.62 mm ring, the stadium keepout its grown ring, every byte
  outside the rewritten spans is unchanged, KiCad's ring orientation and
  closure and the file's newlines are kept, and a second call is a no-op;
* tests/fixtures/dsn/quoted_names.dsn.sexpr (X-01's): a class whose net list
  KiCad wrapped across 3 lines, with "Net-(D1-A)" and "a b";
* synthetic DSNs: holes that merge when grown, a hole that grows into the
  board edge, a keepout with no cutout, a cutout with no keepout, mm units
  with a ' quote character, and every structure error;

plus the no-op and error paths (delta <= 0, frame mismatch, inset errors,
'internal error' re-raised unchanged) and the module's constraints (stdlib +
sibling modules only, no regular expressions, Python 3.11 grammar).

Live tier (skipped without a KiCad whose bundled Python imports pcbnew): the
module imports and rewrites the fixture under the OLDEST install's Python
(KiCad 9.0: 3.11, which runs swig_export_dsn.py), byte-identical to the venv.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import pickle
import subprocess
import sys
from pathlib import Path

import pytest

import dsn_outline as do
import dsn_sexpr as ds
import outline as ol

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent / "server" / "tools"
MODULE = TOOLS / "dsn_outline.py"
FIXTURE = HERE / "fixtures" / "dsn" / "poly_outline_k9.dsn.txt"
QUOTED = HERE / "fixtures" / "dsn" / "quoted_names.dsn.sexpr"

# outline_A.json of the wave-1 T10 spike: the board the fixture was exported from
T10_BOARD = {
    "outline": {"loop": [[4, 0], [56, 0, 0.41421356237309503], [60, 4],
                         [60, 36, 0.41421356237309503], [56, 40],
                         [4, 40, 0.41421356237309503], [0, 36],
                         [0, 4, 0.41421356237309503]]},
    "cutouts": [{"loop": [[27, 7], [27, 11, -1], [33, 11], [33, 7, -1]]},
                {"circle": [30, 20, 2.5]}],
    "datum": [10, 30],
}
T10 = ol.from_board_block(T10_BOARD)
T10_ORIGIN = (118.5, 85.0)
EDGE_MM = 0.3                   # the T10 board's copper-to-edge clearance
DELTA_UM = 120.0                # 0.3 - 0.2 (class clearance) + 0.02 (margin)
QUOTED_BOARD = ol.from_board_block({"w": 30, "h": 20, "cutouts": [{"circle": [15, 10, 1]}]})


# ----------------------------------------------------------------------------
# Readers (through dsn_sexpr, never regexes)
# ----------------------------------------------------------------------------
def _raw(path: Path) -> str:
    """The file exactly as on disk (read_text would translate CRLF)."""
    return path.read_bytes().decode("utf-8")


def _structure(text: str) -> ds.Node:
    return ds.child(ds.parse_nodes(text)[0], "structure")


def _points(shape: ds.Node) -> list[tuple[float, float]]:
    vals = [float(t.value) for t in shape.items[2:]]
    return list(zip(vals[::2], vals[1::2], strict=True))


def _boundary(text: str) -> list[tuple[float, float]]:
    (b,) = ds.find_all(_structure(text), "boundary")
    return _points(ds.child(b, "path"))


def _keepouts(text: str) -> list[list[tuple[float, float]]]:
    return [_points(ds.child(k, "polygon")) for k in ds.find_all(_structure(text), "keepout")]


def _box(pts: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _section(text: str, name: str) -> str:
    node = ds.child(ds.parse_nodes(text)[0], name)
    return text[node.start:node.end]


def _area(pts: list[tuple[float, float]]) -> float:
    return sum(a[0] * b[1] - b[0] * a[1]
               for a, b in zip(pts, pts[1:] + pts[:1], strict=True)) / 2.0


def _centre(pts: list[tuple[float, float]]) -> tuple[float, float]:
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _near(rings: list[list[tuple[float, float]]],
          c: tuple[float, float]) -> list[tuple[float, float]]:
    return min(rings, key=lambda ring: math.dist(_centre(ring), c))


def _seg_dist(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def _origin(text: str, o: ol.Outline) -> tuple[float, float]:
    """The board-frame origin in sheet mm: the DSN boundary bbox minus the outline's."""
    x0, _y0, _x1, y1 = _box(_boundary(text))
    bx0, by0, _bx1, _by1 = ol.bbox(o)
    return (round(x0 / 1000 - bx0, 6), round(-y1 / 1000 - by0, 6))


@pytest.fixture(params=["lf", "crlf"])
def t10_text(request: pytest.FixtureRequest) -> str:
    """The fixture as a Linux (LF) and a Windows (CRLF) KiCad 9 writes it."""
    lf = _raw(FIXTURE).replace("\r\n", "\n")
    return lf if request.param == "lf" else lf.replace("\n", "\r\n")


@pytest.fixture
def t10(t10_text: str) -> tuple[str, str, dict]:
    new, report = do.rewrite_outline(t10_text, T10, _origin(t10_text, T10), EDGE_MM)
    return t10_text, new, report


# ----------------------------------------------------------------------------
# Synthetic KiCad-style DSNs
# ----------------------------------------------------------------------------
def _circle(cx: float, cy: float, r: float, n: int = 32) -> list[tuple[float, float]]:
    return [(cx + r * math.cos(2 * math.pi * k / n), cy - r * math.sin(2 * math.pi * k / n))
            for k in range(n)]


def _path(head: str, pts: list[tuple[float, float]], origin: tuple[float, float],
          per_mm: float) -> str:
    dsn = [((x + origin[0]) * per_mm, -(y + origin[1]) * per_mm) for x, y in [*pts, pts[0]]]
    return f"({head}  " + "  ".join(f"{x:.6g} {y:.6g}" for x, y in dsn) + ")"


def _synth(board: list[tuple[float, float]], holes: list[list[tuple[float, float]]], *,
           origin: tuple[float, float] = (100.0, 50.0), unit: str = "um",
           resolution: str = "um 10", clearance: str = "200", q: str = '"',
           class_rule: str = "") -> str:
    per_mm = {"um": 1000.0, "mm": 1.0}[unit]
    keepouts = [f"    (keepout {q}{q} {_path('polygon signal 0', h, origin, per_mm)})"
                for h in holes]
    lines = ["(pcb synth.dsn", "  (parser", f"    (string_quote {q})",
             f"    (host_cad {q}KiCad Pcbnew{q})", "  )", f"  (resolution {resolution})",
             f"  (unit {unit})", "  (structure", "    (layer F.Cu", "      (type signal)",
             "    )", "    (boundary", "      " + _path("path pcb 0", board, origin, per_mm),
             "    )", *keepouts, "    (rule", f"      (clearance {clearance})",
             f"      (clearance {clearance} (type smd_smd))", "    )", "  )", "  (network",
             f"    (net {q}Net-(D1-A){q}", "      (pins R1-1 D1-1)", "    )",
             f"    (class kicad_default {q}Net-(D1-A){q} {q}a b{q} GND", "      +3V3 VIN",
             "      OUT_A OUT_B", "      (rule", f"        (clearance {clearance})",
             "      )", "    )", *class_rule.splitlines(), "  )", ")"]
    return "\n".join(lines) + "\n"


RECT_40x30 = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)]


# ----------------------------------------------------------------------------
# The fixture and the tokenizer facts the rewrite relies on
# ----------------------------------------------------------------------------
def test_fixture_is_kicad9_export_of_the_t10_board() -> None:
    text = _raw(FIXTURE)
    assert '(host_version "9.0.4")' in text
    assert len(_keepouts(text)) == 2
    assert _origin(text, T10) == T10_ORIGIN
    assert _box(_boundary(text)) == (118500.0, -125000.0, 178500.0, -85000.0)


def test_quoted_names_wrapped_class_and_string_quote_tokenize() -> None:
    tokens = ds.tokenize('(net "Net-(D1-A)" "a b" (pins R1-1))')
    assert [(t.kind, t.value) for t in tokens[1:4]] == [
        ("atom", "net"), ("string", '"Net-(D1-A)"'), ("string", '"a b"')]
    declared = "(pcb x (parser (string_quote ')) (net 'Net-(D1-A)' 'a b'))"
    assert ds.declared_quote(declared) == "'"
    net = ds.child(ds.parse_nodes(declared)[0], "net")
    assert [ds.unquote(t.value, "'") for t in net.items] == ["Net-(D1-A)", "a b"]
    assert ds.quote("Net-(D1-A)", "'") == "'Net-(D1-A)'"
    with pytest.raises(ds.DsnError, match="unterminated"):
        ds.tokenize('(net "Net-(D1-A)\n)')
    with pytest.raises(ValueError, match="overlap"):
        ds.replace_spans("(a b c)", [(1, 4, "x"), (3, 5, "y")])


def test_a_class_wrapped_across_three_lines_with_net_d1_a_parses() -> None:
    text = _raw(QUOTED)
    network = ds.child(ds.parse_nodes(text)[0], "network")
    cls = ds.find_all(network, "class")[0]
    names = [ds.unquote(t.value) for t in cls.items if isinstance(t, ds.Token)]
    assert names[0] == "kicad_default" and "Net-(D1-A)" in names and "a b" in names
    assert len(names) == 10 and text[cls.start:cls.end].count("\n") >= 3
    assert ds.child(ds.child(cls, "rule"), "clearance").items[0].value == "200"
    assert do.min_class_clearance(ds.parse_nodes(text)) == pytest.approx(0.2)
    new, report = do.rewrite_outline(text, QUOTED_BOARD, (100.0, 80.0), EDGE_MM)
    assert report["changed"] and report["holes_replaced"] == 1
    assert _section(new, "network") == _section(text, "network")
    ring = _keepouts(new)[0]
    assert all(abs(math.dist(p, (115000, -90000)) - 1120) < 0.2 for p in ring)


# ----------------------------------------------------------------------------
# min_class_clearance
# ----------------------------------------------------------------------------
def test_min_class_clearance_ignores_typed_clearances(t10_text: str) -> None:
    # (rule (width 200) (clearance 200) (clearance 50 (type smd_smd))): 50 is pad to pad
    assert do.min_class_clearance(ds.parse_nodes(t10_text)) == pytest.approx(0.2)
    assert do.min_class_clearance(ds.parse_nodes(t10_text)[0]) == pytest.approx(0.2)


@pytest.mark.parametrize(("extra", "want"), [
    ("", 0.2),
    ("    (class hv VIN\n      (rule\n        (clearance 150)\n      )\n    )", 0.15),
    ("    (net GND\n      (pins U1-1)\n      (rule (clearance 120))\n    )", 0.12),
    ("    (class hv VIN\n      (rule (clearance 30 (type smd_smd)))\n    )", 0.2),
])
def test_min_class_clearance_takes_every_class_and_net(extra: str, want: float) -> None:
    text = _synth(RECT_40x30, [], class_rule=extra)
    assert do.min_class_clearance(ds.parse_nodes(text)) == pytest.approx(want)


def test_min_class_clearance_converts_the_files_unit() -> None:
    text = _synth(RECT_40x30, [], unit="mm", resolution="mm 10000", clearance="0.25")
    assert do.min_class_clearance(ds.parse_nodes(text)) == pytest.approx(0.25)
    mil = _synth(RECT_40x30, []).replace("(unit um)", "(unit mil)").replace(
        "(clearance 200)", "(clearance 8)")
    assert do.min_class_clearance(ds.parse_nodes(mil)) == pytest.approx(8 * 0.0254)
    upper = _synth(RECT_40x30, []).replace("(unit um)", "(unit UM)")
    assert do.min_class_clearance(ds.parse_nodes(upper)) == pytest.approx(0.2)


# ----------------------------------------------------------------------------
# rewrite_outline on the T10 fixture
# ----------------------------------------------------------------------------
def test_boundary_shrinks_by_delta_per_side(t10: tuple[str, str, dict]) -> None:
    old, new, report = t10
    assert report["changed"] is True and report["reason"] is None
    assert report["delta"] == pytest.approx(0.12)
    assert report["min_clearance_mm"] == pytest.approx(0.2)
    (x0, y0, x1, y1), (n0, m0, n1, m1) = _box(_boundary(old)), _box(_boundary(new))
    for moved in (n0 - x0, m0 - y0, x1 - n1, y1 - m1):
        assert moved == pytest.approx(DELTA_UM, abs=0.2)
    assert report["boundary_points"] == len(_boundary(new))


def test_circle_keepout_becomes_a_2620_um_ring(t10: tuple[str, str, dict]) -> None:
    _old, new, report = t10
    centre = ((30 + T10_ORIGIN[0]) * 1000, -(20 + T10_ORIGIN[1]) * 1000)
    ring = _near(_keepouts(new), centre)
    assert len(ring) > 16
    assert all(abs(math.dist(p, centre) - 2620) <= 0.2 for p in ring)        # on the arc
    mids = [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in zip(ring[:-1], ring[1:],
                                                                    strict=True)]
    assert all(2620 - 5 - 0.2 <= math.dist(m, centre) for m in mids)        # max_error
    assert report["holes_replaced"] == 2 and report["holes_unmatched"] == 0
    assert report["keepouts_deleted"] == 0 and report["warnings"] == []


def test_stadium_keepout_is_replaced_by_its_grown_ring(t10: tuple[str, str, dict]) -> None:
    old, new, _report = t10
    a, b = (148500.0, -92000.0), (148500.0, -96000.0)       # (30, 7)-(30, 11) + origin
    before, after = _near(_keepouts(old), a), _near(_keepouts(new), a)
    assert all(abs(_seg_dist(p, a, b) - 3000) < 5 for p in before)     # KiCad's arc error
    assert all(abs(_seg_dist(p, a, b) - 3120) <= 0.2 for p in after)
    assert len(_keepouts(new)) == 2


def test_every_byte_outside_the_rewritten_spans_is_unchanged(t10: tuple[str, str, dict]) -> None:
    old, new, _report = t10
    for name in ("parser", "placement", "library", "network", "wiring"):
        assert _section(new, name) == _section(old, name), name

    def outside(text: str) -> list[str]:
        st = _structure(text)
        spans = [ds.child(b, "path") for b in ds.find_all(st, "boundary")]
        spans += [ds.child(k, "polygon") for k in ds.find_all(st, "keepout")]
        cuts = [0, *[i for n in sorted(spans, key=lambda n: n.start) for i in (n.start, n.end)],
                len(text)]
        return [text[cuts[i]:cuts[i + 1]] for i in range(0, len(cuts), 2)]

    assert outside(new) == outside(old)
    assert new != old


def test_rings_keep_kicads_orientation_closure_and_newlines(t10: tuple[str, str, dict]) -> None:
    old, new, _report = t10
    pairs = [(k, _near(_keepouts(new), _centre(k))) for k in _keepouts(old)]
    for before, after in [(_boundary(old), _boundary(new)), *pairs]:
        assert after[0] == after[-1]
        assert (_area(before) > 0) == (_area(after) > 0)       # boundary CW, holes CCW (DSN)
    crlf = "\r\n" in old
    assert new.count("\n") == new.count("\r\n") if crlf else "\r" not in new
    fresh = set(new.splitlines()) - set(old.splitlines())
    assert fresh and all(len(line) <= 100 for line in fresh)
    assert all(line.startswith(" " * 12) or "(path" in line or "(polygon" in line
               for line in fresh)


def test_a_second_rewrite_is_a_noop(t10: tuple[str, str, dict]) -> None:
    _old, new, _report = t10
    again, report = do.rewrite_outline(new, T10, _origin(_old, T10), EDGE_MM)
    assert again == new
    assert report["changed"] is False and report["reason"] == "already rewritten"


def test_the_raw_fixture_rewrites_whatever_its_checkout_newlines() -> None:
    text = _raw(FIXTURE)
    new, report = do.rewrite_outline(text, T10, T10_ORIGIN, EDGE_MM)
    assert report["changed"] and _box(_boundary(new))[0] == 118620.0


# ----------------------------------------------------------------------------
# No-ops and errors
# ----------------------------------------------------------------------------
@pytest.mark.parametrize(("edge", "margin"), [(0.1, 0.02), (0.2, 0.0)])
def test_delta_at_or_below_zero_is_a_noop(t10_text: str, edge: float, margin: float) -> None:
    new, report = do.rewrite_outline(t10_text, T10, T10_ORIGIN, edge, margin=margin)
    assert new is t10_text and report["changed"] is False
    assert report["delta"] <= 0 and "delta" in report["reason"]


@pytest.mark.parametrize("origin", [(119.5, 85.0), (118.5, 84.99), (0.0, 0.0)])
def test_a_frame_mismatch_raises(t10_text: str, origin: tuple[float, float]) -> None:
    with pytest.raises(do.DsnOutlineError, match=r"^frame mismatch") as err:
        do.rewrite_outline(t10_text, T10, origin, EDGE_MM)
    assert err.value.kind == "frame"
    again = pickle.loads(pickle.dumps(err.value))       # survives a process boundary
    assert (type(again), str(again), again.kind) == (do.DsnOutlineError, str(err.value), "frame")


def test_another_board_or_another_delta_is_a_frame_mismatch(t10_text: str) -> None:
    wider = ol.check(ol.Outline(ol.rect_loop(61, 40, 4), T10.cutouts))
    with pytest.raises(do.DsnOutlineError, match=r"^frame mismatch"):
        do.rewrite_outline(t10_text, wider, T10_ORIGIN, EDGE_MM)
    new, _report = do.rewrite_outline(t10_text, T10, T10_ORIGIN, EDGE_MM)
    with pytest.raises(do.DsnOutlineError, match="another delta"):
        do.rewrite_outline(new, T10, T10_ORIGIN, 0.5)


def test_an_inset_failure_is_a_dsn_outline_error(t10_text: str) -> None:
    with pytest.raises(do.DsnOutlineError, match="consumes the whole board") as err:
        do.rewrite_outline(t10_text, T10, T10_ORIGIN, 21.0)
    assert err.value.kind == "inset" and isinstance(err.value, ValueError)
    with pytest.raises(do.DsnOutlineError, match=r"^frame mismatch"):  # never hidden by it
        do.rewrite_outline(t10_text, T10, (100.0, 85.0), 21.0)


def test_an_internal_inset_error_is_reraised_unchanged(t10_text: str,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    bug = ol.OutlineError("internal error: the inset by 0.12 mm put outline 0.1 mm away")

    def broken(_o: ol.Outline, _d: float) -> ol.Outline:
        raise bug

    monkeypatch.setattr(do, "inset", broken)
    with pytest.raises(ol.OutlineError) as err:
        do.rewrite_outline(t10_text, T10, T10_ORIGIN, EDGE_MM)
    assert err.value is bug and not isinstance(err.value, do.DsnOutlineError)


def test_parse_errors_stay_dsn_errors() -> None:
    with pytest.raises(ds.DsnError, match="unterminated"):
        do.rewrite_outline('(pcb x\n  (net "a\n)\n', T10, T10_ORIGIN, EDGE_MM)


_STRUCTURE_ERRORS = [
    ("(boundary\n      (path pcb 0", "(boundary\n      (rect pcb 0 0 1 1) (path pcb 0", "boundary"),
    ("    (boundary\n", "    (bound\n", "boundary"),
    ("(path pcb 0  ", "(path pcb 0  1 ", "path"),
    ("      (clearance 200)\n", "", None),
    ("  (unit um)\n", "", None),
    ("(resolution um 10)", "(resolution furlong 10)", "unit"),
    ("(pcb synth.dsn", "(pcb synth.dsn) (pcb", "pcb"),
]


@pytest.mark.parametrize(("old", "new", "word"), _STRUCTURE_ERRORS)
def test_structure_errors_raise(old: str, new: str, word: str | None) -> None:
    text = _synth(RECT_40x30, [_circle(20, 15, 1)]).replace(old, new, 1)
    if old == "      (clearance 200)\n":
        text = text.replace("        (clearance 200)\n", "")
    if old == "  (unit um)\n":
        text = text.replace("  (resolution um 10)\n", "")
    with pytest.raises(do.DsnOutlineError) as err:
        do.rewrite_outline(text, ol.Outline(ol.rect_loop(40, 30)), (100.0, 50.0), EDGE_MM)
    assert err.value.kind == "structure" and (word is None or word in str(err.value))


@pytest.mark.parametrize(("args", "exc"), [
    (("not an outline", T10_ORIGIN, EDGE_MM), TypeError),
    ((T10, (1.0,), EDGE_MM), ValueError),
    ((T10, (1.0, float("nan")), EDGE_MM), ValueError),
    ((T10, T10_ORIGIN, -0.1), ValueError),
    ((T10, T10_ORIGIN, True), ValueError),
])
def test_bad_arguments_raise(t10_text: str, args: tuple, exc: type) -> None:
    with pytest.raises(exc):
        do.rewrite_outline(t10_text, *args)
    with pytest.raises(ValueError, match="max_error"):
        do.rewrite_outline(t10_text, T10, T10_ORIGIN, EDGE_MM, max_error=0)


# ----------------------------------------------------------------------------
# Holes that merge, reach the edge, or have no counterpart
# ----------------------------------------------------------------------------
def test_merged_bay_unmatched_and_missing_holes() -> None:
    cuts = (ol.Circle(10, 10, 1), ol.Circle(12.1, 10, 1), ol.Circle(30, 15, 1.5),
            ol.Circle(1.1, 20, 1), ol.Circle(20, 10, 1))
    board = ol.check(ol.Outline(ol.rect_loop(40, 30), cuts))
    holes = [_circle(10, 10, 1), _circle(12.1, 10, 1), _circle(30, 15, 1.5),
             _circle(20, 25, 0.5), _circle(1.1, 20, 1)]   # no keepout for (20, 10)
    text = _synth(RECT_40x30, holes)
    new, report = do.rewrite_outline(text, board, (100.0, 50.0), EDGE_MM)
    assert (report["holes_replaced"], report["keepouts_deleted"],
            report["holes_unmatched"]) == (2, 1, 2)
    words = " | ".join(report["warnings"])
    for needle in ("merged", "no cutout within 0.5 mm", "board edge", "no keepout"):
        assert needle in words, words
    old_k, new_k = _keepouts(text), _keepouts(new)
    assert len(new_k) == 4 and new_k[2:] == old_k[3:]          # the last two untouched
    c1, c2 = (110000.0, -60000.0), (112100.0, -60000.0)
    assert all(min(math.dist(p, c1), math.dist(p, c2)) >= 1120 - 0.2 for p in new_k[0])
    assert min(math.dist(p, c1) for p in new_k[0]) == pytest.approx(1120, abs=0.2)
    assert all(abs(math.dist(p, (130000, -65000)) - 1620) <= 0.2 for p in new_k[1])
    assert _box(_boundary(new)) == (100120.0, -79880.0, 139880.0, -50120.0)
    assert _section(new, "network") == _section(text, "network")
    assert do.rewrite_outline(new, board, (100.0, 50.0), EDGE_MM)[0] == new


def test_mm_units_and_a_single_quote_character() -> None:
    text = _synth(RECT_40x30, [_circle(20, 15, 1)], unit="mm", resolution="mm 10000",
                  clearance="0.2", q="'")
    board = ol.Outline(ol.rect_loop(40, 30), (ol.Circle(20, 15, 1),))
    new, report = do.rewrite_outline(text, board, (100.0, 50.0), EDGE_MM)
    assert report["changed"] and report["holes_replaced"] == 1
    assert _box(_boundary(new)) == pytest.approx((100.12, -79.88, 139.88, -50.12), abs=1e-4)
    ring = _keepouts(new)[0]
    assert all(abs(math.dist(p, (120.0, -65.0)) - 1.12) <= 1e-4 for p in ring)
    words = [t.value for t in ds.tokenize(_section(new, "structure"))
             if t.kind == "atom" and t.value[:1] in "-0123456789"]
    assert all(len(w.partition(".")[2]) <= 4 for w in words)
    assert _section(new, "network") == _section(text, "network")


# ----------------------------------------------------------------------------
# Module constraints
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["dsn_outline.py", "dsn_sexpr.py"])
def test_modules_are_stdlib_only_regex_free_and_py311(name: str) -> None:
    src = (TOOLS / name).read_text(encoding="utf-8")
    assert len(src.splitlines()) < 800
    tree = ast.parse(src, feature_version=(3, 11))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}
    assert "re" not in imported
    siblings = {p.stem for p in TOOLS.glob("*.py")}
    assert imported <= set(sys.stdlib_module_names) | {"__future__", "dsn_sexpr", "outline"}
    assert imported & siblings <= {"dsn_sexpr", "outline"}


# ----------------------------------------------------------------------------
# Live tier: the oldest KiCad install's bundled Python (KiCad 9.0: 3.11)
# ----------------------------------------------------------------------------
_LIVE = """import hashlib, json, sys
sys.path.insert(0, sys.argv[1])
import dsn_outline, outline
board = outline.from_board_block(json.loads(sys.argv[3]))
text = open(sys.argv[2], encoding="utf-8", newline="").read()
new, report = dsn_outline.rewrite_outline(text, board, json.loads(sys.argv[4]), 0.3)
print(json.dumps({"python": list(sys.version_info[:2]), "report": report,
                  "sha256": hashlib.sha256(new.encode("utf-8")).hexdigest()}))
"""


@pytest.fixture(scope="module")
def oldest_kicad_python() -> str:
    import kicad_env as env
    found = [i for i in env.discover() if i.python is not None]
    if not found:
        pytest.skip(f"no KiCad python with pcbnew: {env.probe_errors()}")
    return str(min(found, key=lambda i: i.version_tuple).python)


def test_live_rewrite_under_kicad_python_matches_the_venv(oldest_kicad_python: str) -> None:
    argv = [oldest_kicad_python, "-c", _LIVE, str(TOOLS), str(FIXTURE), json.dumps(T10_BOARD),
            json.dumps(T10_ORIGIN)]
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=180, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(r.stdout.strip().splitlines()[-1])
    new, report = do.rewrite_outline(_raw(FIXTURE), T10, T10_ORIGIN, EDGE_MM)
    assert got["python"][0] == 3 and got["report"] == json.loads(json.dumps(report))
    assert got["sha256"] == hashlib.sha256(new.encode("utf-8")).hexdigest()
