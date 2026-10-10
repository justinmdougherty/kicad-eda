"""board_fingerprint (F5-15): one sha256 over a normalised view of a .kicad_pcb.

Boards are built here as text in the KiCad 9 (numbered net table), KiCad 10
(net names inline) and KiCad 11 nightly (footprint transform) dialects, with a
fresh uuid on every item of every build, so two builds of the same design
always differ in every uuid / tstamp and must still fingerprint the same.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Callable

import pytest

import board_fingerprint as bf

ROOT = Path(__file__).resolve().parents[1]
NETS_K9 = {"SIG": 1, "GND": 2, "VCC": 3}
FILL = [(0.5, 0.5), (29.5, 0.5), (29.5, 19.5), (0.5, 19.5)]
NetFmt = Callable[[str], str]


def _uuid() -> str:
    return f'(uuid "{uuid.uuid4()}")'


def k9_net(table: dict[str, int] = NETS_K9) -> NetFmt:
    return lambda name: f"(net {table[name]})"


def k10_net(name: str) -> str:
    return f'(net "{name}")'


def fp_at(x: float, y: float, rot: float | None) -> str:
    return f"(at {x} {y})" if rot is None else f"(at {x} {y} {rot})"


def fp_transform(x: float, y: float, rot: float | None) -> str:
    return f"(transform (translate {x} {y}) (rotate {rot or 0}) (scale 1 1))"


def footprint(ref: str, lib: str, x: float, y: float, rot: float | None, layer: str,
              net: NetFmt, place: Callable[..., str] = fp_at) -> str:
    return (f'(footprint "{lib}" (layer "{layer}") {_uuid()} {place(x, y, rot)} '
            f'(property "Reference" "{ref}" (at 0 -1.4 0) (layer "F.SilkS") {_uuid()}) '
            f'(property "Value" "10k" (at 0 1.4 0) (layer "F.Fab") {_uuid()}) '
            f'(pad "1" smd roundrect (at -0.8 0) (size 0.8 0.9) (layers "{layer}") '
            f'{net("SIG")} {_uuid()}) '
            f'(pad "2" smd roundrect (at 0.8 0) (size 0.8 0.9) (layers "{layer}") '
            f'{net("GND")} {_uuid()}))')


def items(net: NetFmt, zone_net: NetFmt, *, via_at: tuple[float, float] = (10.0, 5.0),
          width: float = 0.25, rot: float | None = 90, seg_reversed: bool = False,
          fill: list[tuple[float, float]] = FILL, place: Callable[..., str] = fp_at,
          seg_net: str = "SIG", jitter: float = 0.0) -> list[str]:
    a, b = (f"{1 + jitter} 2", "8 2") if not seg_reversed else ("8 2", f"{1 + jitter} 2")
    pts = " ".join(f"(xy {x} {y})" for x, y in fill)
    return [
        footprint("R1", "Resistor_SMD:R_0603_1608Metric", 5, 5, rot, "F.Cu", net, place),
        footprint("U1", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", 20, 10, None, "B.Cu", net, place),
        f'(segment (start {a}) (end {b}) (width {width}) (layer "F.Cu") {net(seg_net)} '
        f'{_uuid()})',
        f'(segment (start 8 2) (end 10 5) (width 0.25) (layer "F.Cu") {net("SIG")} '
        f'(tstamp "{uuid.uuid4()}"))',
        f'(arc (start 10 5) (mid 11 6) (end 12 5) (width 0.25) (layer "B.Cu") {net("SIG")} '
        f'{_uuid()})',
        f'(via (at {via_at[0]} {via_at[1]}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
        f'{net("SIG")} {_uuid()})',
        f'(via (at 15 15) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") {net("GND")} '
        f'{_uuid()})',
        f'(zone {zone_net("GND")} (layer "B.Cu") {_uuid()} (hatch edge 0.5) '
        f'(connect_pads (clearance 0.5)) (min_thickness 0.25) '
        f'(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5)) '
        f'(polygon (pts (xy 0 0) (xy 30 0) (xy 30 20) (xy 0 20))) '
        f'(filled_polygon (layer "B.Cu") (pts {pts})))',
    ]


def k9_zone_net(table: dict[str, int] = NETS_K9) -> NetFmt:
    return lambda name: f'(net {table[name]}) (net_name "{name}")'


def board(body: list[str], *, version: int = 20241229, gen: str = "9.0",
          table: dict[str, int] | None = NETS_K9, title: str = "A") -> str:
    nets = ("" if table is None else '(net 0 "") ' + " ".join(
        f'(net {num} "{name}")' for name, num in table.items()))
    return (f'(kicad_pcb (version {version}) (generator "pcbnew") (generator_version "{gen}") '
            f'(general (thickness 1.6)) (paper "A4") '
            f'(title_block (title "{title}") (date "2026-10-0{len(title)}")) '
            f'(layers (0 "F.Cu" signal) (2 "B.Cu" signal)) (setup (pad_to_mask_clearance 0)) '
            f'{nets} {" ".join(body)} '
            f'(gr_line (start 0 0) (end 30 0) (stroke (width 0.1) (type default)) '
            f'(layer "Edge.Cuts") {_uuid()}))')


def k9_board(**kw: object) -> str:
    return board(items(k9_net(), k9_zone_net(), **kw))


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def same(tmp_path: Path, a: str, b: str) -> bool:
    fa = bf.fingerprint(write(tmp_path, "a.kicad_pcb", a))
    fb = bf.fingerprint(write(tmp_path, "b.kicad_pcb", b))
    return fa == fb


def diff_lines(tmp_path: Path, a: str, b: str) -> list[str]:
    return bf.diff(write(tmp_path, "a.kicad_pcb", a), write(tmp_path, "b.kicad_pcb", b))


# --- the same design always fingerprints the same -------------------------------------

def test_item_order_uuids_tstamps_and_header_do_not_matter(tmp_path: Path) -> None:
    a = k9_board()
    b = board(list(reversed(items(k9_net(), k9_zone_net()))), gen="9.0.4", title="Other")
    assert a != b
    fa = bf.fingerprint(write(tmp_path, "a.kicad_pcb", a))
    assert fa == bf.fingerprint(write(tmp_path, "b.kicad_pcb", b))
    assert fa["counts"] == {"segments": 2, "arcs": 1, "vias": 2, "footprints": 2, "zones": 1}
    assert len(fa["sha256"]) == 64 and int(fa["sha256"], 16) >= 0


def test_segment_direction_and_sub_rounding_noise_do_not_matter(tmp_path: Path) -> None:
    assert same(tmp_path, k9_board(), k9_board(seg_reversed=True, jitter=0.00001))


def test_nets_are_compared_by_name_not_number(tmp_path: Path) -> None:
    renumbered = {"VCC": 1, "SIG": 2, "GND": 3}
    b = board(items(k9_net(renumbered), k9_zone_net(renumbered)), table=renumbered)
    assert same(tmp_path, k9_board(), b)
    k10 = board(items(k10_net, k10_net), version=20260206, gen="10.0", table=None)
    assert same(tmp_path, k9_board(), k10)


def test_rotation_is_modulo_360_and_the_k11_transform_reads_like_at(tmp_path: Path) -> None:
    assert same(tmp_path, k9_board(rot=-90), k9_board(rot=270))
    assert same(tmp_path, k9_board(rot=0), k9_board(rot=None))
    k11 = board(items(k10_net, k10_net, place=fp_transform), version=20260901, gen="10.99",
                table=None)
    assert same(tmp_path, k9_board(), k11)


def test_filled_polygon_point_order_does_not_matter(tmp_path: Path) -> None:
    assert same(tmp_path, k9_board(), k9_board(fill=FILL[2:] + FILL[:2]))


# --- a real change changes the sha, and diff names it ---------------------------------

def test_a_moved_via_changes_the_sha_and_diff_names_it(tmp_path: Path) -> None:
    a, b = k9_board(), k9_board(via_at=(10.5, 5.0))
    assert not same(tmp_path, a, b)
    lines = diff_lines(tmp_path, a, b)
    assert lines == ["via SIG at (10, 5): at (10, 5) -> (10.5, 5)"]


def test_a_changed_width_changes_the_sha_and_diff_names_it(tmp_path: Path) -> None:
    a, b = k9_board(), k9_board(width=0.3)
    assert not same(tmp_path, a, b)
    (line,) = diff_lines(tmp_path, a, b)
    assert line.startswith("segment SIG F.Cu (1, 2)-(8, 2)") and "width 0.25 -> 0.3" in line


def test_a_moved_or_rotated_footprint_is_named_by_reference(tmp_path: Path) -> None:
    (line,) = diff_lines(tmp_path, k9_board(), k9_board(rot=0))
    assert line == "footprint R1 (Resistor_SMD:R_0603_1608Metric): rot 90 -> 0"


def test_a_net_change_and_a_fill_change_are_named(tmp_path: Path) -> None:
    (line,) = diff_lines(tmp_path, k9_board(), k9_board(seg_net="GND"))
    assert "net SIG -> GND" in line
    moved = [(0.5, 0.5), (29.5, 0.5), (29.5, 19.25), (0.5, 19.5)]
    (fill,) = diff_lines(tmp_path, k9_board(), k9_board(fill=moved))
    assert fill.startswith("zone GND on B.Cu: filled differs")


def test_added_and_removed_items_are_counted_and_listed(tmp_path: Path) -> None:
    body = items(k9_net(), k9_zone_net())
    extra = f'(segment (start 0 0) (end 3 4) (width 0.5) (layer "B.Cu") (net 3) {_uuid()})'
    lines = diff_lines(tmp_path, board(body), board([*body, extra]))
    assert lines == ["segments: 2 in A, 3 in B",
                     "segment VCC B.Cu (0, 0)-(3, 4) width 0.5: only in B"]
    lines = diff_lines(tmp_path, board([*body, extra]), board(body))
    assert lines[-1].endswith(": only in A")


def test_diff_of_identical_boards_is_empty_and_the_limit_holds(tmp_path: Path) -> None:
    assert diff_lines(tmp_path, k9_board(), k9_board()) == []
    many = [f'(via (at {i} 1) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) {_uuid()})'
            for i in range(30)]
    a, b = (write(tmp_path, n, board(v)) for n, v in (("a.kicad_pcb", many),
                                                       ("b.kicad_pcb", many[:5])))
    assert len(bf.diff(a, b)) == bf.DIFF_LIMIT
    assert len(bf.diff(a, b, limit=3)) == 3


def test_a_real_pipeline_board(tmp_path: Path) -> None:
    """A board the harness wrote (generated, git-ignored: skipped when absent): the
    counts match its top-level items, and new uuids leave the sha unchanged."""
    real = ROOT / "examples" / "lm358_buffer" / "netlist.kicad_pcb"
    if not real.is_file():
        pytest.skip("no generated examples/lm358_buffer board (run the pipeline on it)")
    text = real.read_text(encoding="utf-8")
    fp = bf.fingerprint(real)
    for head, key in (("segment", "segments"), ("arc", "arcs"), ("via", "vias"),
                      ("footprint", "footprints"), ("zone", "zones")):
        assert fp["counts"][key] == len(re.findall(rf"\n\t\({head}\s", text)), key
    fresh = re.sub(r'\(uuid "[^"]+"\)', lambda _m: _uuid(), text)
    assert fresh != text
    assert bf.fingerprint(write(tmp_path, "fresh.kicad_pcb", fresh)) == fp


# --- errors -----------------------------------------------------------------------------

def test_errors_are_loud(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a KiCad board"):
        bf.fingerprint(write(tmp_path, "s.kicad_sch", "(kicad_sch (version 1))"))
    with pytest.raises(ValueError, match="unbalanced"):
        bf.fingerprint(write(tmp_path, "u.kicad_pcb", k9_board()[:-1]))
    with pytest.raises(ValueError, match="net 7"):
        bf.fingerprint(write(tmp_path, "n.kicad_pcb", board(
            [f'(via (at 1 1) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 7) {_uuid()})'])))
    with pytest.raises(FileNotFoundError):
        bf.fingerprint(tmp_path / "missing.kicad_pcb")


# --- command line -----------------------------------------------------------------------

def test_cli_one_board_prints_its_fingerprint(tmp_path: Path,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path, "a.kicad_pcb", k9_board())
    assert bf.main([str(path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"path": str(path), **bf.fingerprint(path)}


def test_cli_two_boards(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a = write(tmp_path, "a.kicad_pcb", k9_board())
    b = write(tmp_path, "b.kicad_pcb", k9_board())
    c = write(tmp_path, "c.kicad_pcb", k9_board(via_at=(11.0, 5.0)))
    assert bf.main([str(a), str(b)]) == 0
    assert "identical" in capsys.readouterr().out
    assert bf.main([str(a), str(c)]) == 1
    assert "via SIG at (10, 5): at (10, 5) -> (11, 5)" in capsys.readouterr().out
    assert bf.main([str(a), str(tmp_path / "missing.kicad_pcb")]) == 2
    assert "board_fingerprint:" in capsys.readouterr().err
    assert bf.main([]) == 2


# --- the contract -----------------------------------------------------------------------

def test_module_doc_lists_what_is_and_is_not_compared() -> None:
    doc = bf.__doc__ or ""
    assert "Compared" in doc and "Not compared" in doc
    for word in ("segments", "arcs", "vias", "footprints", "zones", "uuid", "tstamp",
                 "generator", "version", "title block"):
        assert word in doc, word


@pytest.mark.parametrize("rel", ["server/tools/board_fingerprint.py",
                                 "pipeline/determinism_check.py"])
def test_files_stay_under_400_lines(rel: str) -> None:
    lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
    assert len(lines) < 400, f"{rel}: {len(lines)} lines"
