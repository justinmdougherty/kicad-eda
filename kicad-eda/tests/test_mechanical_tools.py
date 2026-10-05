"""mechanical_tools: the F3 MCP surface (ROADMAP D2/D3) and tools/mech_export.py.

Pure unit tier (.venv): toolchain.run / board_cli are faked, so no KiCad is
discovered or run. A fake kicad-cli writes what the real one would (a DXF from
dxf_io, a tiny STEP, a pos CSV) at the ``-o`` path, and every command line is
asserted exactly. DXFs come from tests/fixtures/dxf_builders.py and dxf_io;
the reference board is a handwritten .kicad_pcb with the T10 outline drawn at
an offset and its aux origin on the datum (10, 30).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import dxf_io
import mech_export as mx
import mechanical_tools as mt
import outline as ol
import toolchain as tc
from fixtures.dxf_builders import Sketch, fusion_board, fusion_doc, new_doc, save
from fixtures.swig_python import T10_BOARD

CLI = "kicad-cli"
OX, OY = 100.5, 115.0                 # where the T10 board sits on the sheet
DXF_HELP = """Usage: dxf [--help] [--output OUTPUT_FILE] [--layers LAYER_LIST] INPUT_FILE
  -o, --output                                     Output file
  -l, --layers                                     Comma separated list of layer names
  --uc, --use-contours                             Plot graphic items using their contours
  --udo, --use-drill-origin                        Plot using the drill/place file origin
  --ou, --output-units                             Output units, valid options: mm, in
  --drill-shape-opt                                Set pad/via drill shape option
  --mode-single                                    Generates a single file
  --mode-multi                                     Generates one or more files
"""
DXF_CMD = ["pcb", "export", "dxf", "--layers", "Edge.Cuts", "--use-drill-origin", "--ou", "mm",
           "--drill-shape-opt", "0", "--mode-single", "-o"]
TINY_STEP = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('tiny; board'),'2;1');
ENDSEC;
DATA;
#1 = ( LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT({prefix},.METRE.) );
#10 = CARTESIAN_POINT('',(0.,0.,0.));
#11 = DIRECTION('',(0.,0.,1.));
#12 = DIRECTION('',(1.,0.,0.));
#13 = AXIS2_PLACEMENT_3D('',#10,#11,#12);
#20 = CARTESIAN_POINT('',({x0},{y0},0.));
#21 = VERTEX_POINT('',#20);
#22 = CARTESIAN_POINT('',
  ({x1},{y1},{z1}));
#23 = VERTEX_POINT('a '';'' name',#22);
#30 = CARTESIAN_POINT('far placement',(900.,900.,900.));
#31 = CARTESIAN_POINT('',(7.5,2.5));
ENDSEC;
END-ISO-10303-21;
"""


# --- fakes and builders -------------------------------------------------------------------
class FakeCli:
    """Stands in for toolchain.run: records every call, answers --help with
    `help_text` and writes the -o file through `write` (None = writes nothing)."""

    def __init__(self, write=None, help_text: str = DXF_HELP, rc: int = 0,
                 output: str = "Done.\n") -> None:
        self.write, self.help_text, self.rc, self.output = write, help_text, rc, output
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args: list[str], **kw) -> subprocess.CompletedProcess:
        self.calls.append((list(args), kw))
        if "--help" in args:
            return subprocess.CompletedProcess(args, 1, self.help_text, "")
        if self.write is not None:
            self.write(Path(args[args.index("-o") + 1]))
        return subprocess.CompletedProcess(args, self.rc, self.output, "")

    @property
    def jobs(self) -> list[list[str]]:
        return [a for a, _kw in self.calls if "--help" not in a]


@pytest.fixture(autouse=True)
def no_kicad(monkeypatch) -> None:
    """No KiCad is discovered: board_cli is a fixed name, run fails loudly."""
    monkeypatch.setattr(tc, "board_cli", lambda pcb="": CLI)
    monkeypatch.setattr(tc, "board_cli_note", lambda pcb="": None)

    def no_run(*_a, **_k):
        raise AssertionError("tc.run called without a FakeCli")

    monkeypatch.setattr(tc, "run", no_run)


def _cli(monkeypatch, **kw) -> FakeCli:
    fake = FakeCli(**kw)
    monkeypatch.setattr(tc, "run", fake)
    return fake


def _fmt(v: float) -> str:
    return repr(round(float(v), 9))


def _pcb_text(block: dict, ox: float = OX, oy: float = OY) -> str:
    """A minimal KiCad 9 board: the block's outline as exact Edge.Cuts shapes at
    (ox, oy) and the aux origin on its datum."""
    model = ol.from_board_block(block)

    def pt(key: str, p) -> str:
        return f"({key} {_fmt(p[0] + ox)} {_fmt(p[1] + oy)})"

    items = []
    for p in ol.to_kicad_primitives(model):
        tail = '(stroke (width 0.1) (type default)) (layer "Edge.Cuts")'
        if p["kind"] == "line":
            items.append(f"(gr_line {pt('start', p['start'])} {pt('end', p['end'])} {tail})")
        elif p["kind"] == "arc":
            items.append(f"(gr_arc {pt('start', p['start'])} {pt('mid', p['mid'])} "
                         f"{pt('end', p['end'])} {tail})")
        else:
            (cx, cy), r = p["center"], p["radius"]
            items.append(f"(gr_circle {pt('center', (cx, cy))} {pt('end', (cx + r, cy))} {tail})")
    dx, dy = model.datum if model.datum is not None else (-ox, -oy)
    return ('(kicad_pcb (version 20241229) (generator "pcbnew") (generator_version "9.0")\n'
            f"  (setup (aux_axis_origin {_fmt(dx + ox)} {_fmt(dy + oy)}))\n  "
            + "\n  ".join(items) + ")\n")


@pytest.fixture
def t10_pcb(tmp_path: Path) -> Path:
    pcb = tmp_path / "t10.kicad_pcb"
    pcb.write_text(_pcb_text(T10_BOARD), encoding="utf-8")
    return pcb


def _t10_dxf(path: Path, layer: str = "OUTLINE") -> Path:
    """The T10 board as a dxf_builders sketch: Y up, origin = the datum (10, 30)."""
    doc, msp = new_doc(4)
    sk = Sketch(msp, layer=layer)
    sk.rounded_rect(-10, -10, 50, 30, 4)
    sk.line((17, 23), (17, 19))                 # the vertical stadium: board x 27..33, with
    sk.arc((20, 19), 3, 180, 360)               # r=3 ends centred on (30, 7) and (30, 11)
    sk.line((23, 19), (23, 23))
    sk.arc((20, 23), 3, 0, 180)
    sk.circle((20, 10), 2.5)                    # board hole (30, 20)
    return _save(doc, path.parent, path.name)


def _save(doc, folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    return save(doc, folder, name)


COMPONENTS = [
    {"ref": "J1", "lib_id": "Connector:Conn_01x02", "footprint": "C:J", "x": 3, "y": 4, "rot": 90,
     "value": "PWR"},
    {"ref": "U1", "lib_id": "MCU:U", "footprint": "P:U", "at": [1, 2, 180]},
    {"ref": "R1", "lib_id": "Device:R", "footprint": "R:R"},
]


def _netlist(tmp_path: Path, board: dict | None = None, **extra) -> Path:
    design = {"name": "mech", "board": board if board is not None else
              {"w": 10, "h": 10, "corner_radius": 1, "auto_x": 2},
              "components": [dict(c) for c in COMPONENTS], "nets": {"GND": [["J1", "1"]]},
              **extra}
    path = tmp_path / "netlist.json"
    path.write_text(json.dumps(design, indent=2) + "\n", encoding="utf-8")
    return path


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _part(design: dict, ref: str) -> dict:
    return next(c for c in design["components"] if c["ref"] == ref)


# --- registry -----------------------------------------------------------------------------
def test_tools_tuple_lists_the_six_mechanical_tools_with_docs():
    assert [fn.__name__ for fn in mt.TOOLS] == [
        "import_outline_dxf", "write_outline_dxf", "export_outline_dxf", "export_step",
        "export_pos_datum", "validate_board_outline"]
    assert all(len(fn.__doc__ or "") > 200 for fn in mt.TOOLS)
    assert "ROADMAP_DECISIONS" in mt.__doc__ and "TOOLS" in mt.__doc__


@pytest.mark.parametrize("call", [
    lambda p: mt.import_outline_dxf(str(p / "missing.json"), "x.dxf"),
    lambda p: mt.write_outline_dxf(str(p / "missing.json"), str(p / "o.dxf")),
    lambda p: mt.export_outline_dxf(str(p / "missing.kicad_pcb"), str(p / "o.dxf")),
    lambda p: mt.export_step(str(p / "missing.kicad_pcb"), str(p / "o.step")),
    lambda p: mt.export_pos_datum(str(p / "missing.kicad_pcb"), str(p / "o.csv")),
    lambda p: mt.validate_board_outline(str(p / "missing.json"))])
def test_every_tool_reports_a_missing_input_as_not_ok(call, tmp_path):
    res = call(tmp_path)
    assert res["ok"] is False and "not found" in res["reason"] and "missing" in res["reason"]


# --- import_outline_dxf -------------------------------------------------------------------
def test_import_reference_mode_writes_a_relative_dxf_reference(tmp_path):
    doc, msp = fusion_doc()                                  # Fusion: cm, sketch layer
    fusion_board(Sketch(msp, k=10.0))
    _save(doc, tmp_path / "mech", "outline.dxf")
    netlist = _netlist(tmp_path)
    before = netlist.read_bytes()
    res = mt.import_outline_dxf(str(netlist), "mech/outline.dxf")
    assert res["ok"] is True, res
    board = _read(netlist)["board"]
    assert board == {"auto_x": 2, "outline": {"dxf": "mech/outline.dxf"}}
    assert res["mode"] == "reference" and res["source"] == "dxf"
    assert res["bbox"] == pytest.approx([0, 0, 60, 40]) and res["datum"] == pytest.approx([10, 10])
    assert any("w" in n and "corner_radius" in n for n in res["notes"])
    assert Path(res["backup"]) == tmp_path / "netlist.json.bak"
    assert Path(res["backup"]).read_bytes() == before
    assert isinstance(res["fab_issues"], list) and isinstance(res["warnings"], list)


def test_import_reference_mode_keeps_layer_and_units_when_given(tmp_path):
    doc, msp = fusion_doc()
    fusion_board(Sketch(msp, k=10.0))
    dxf = save(doc, tmp_path, "board.dxf")
    netlist = _netlist(tmp_path, {"w": 60, "h": 40})
    res = mt.import_outline_dxf(str(netlist), str(dxf), layer="board v3_Sketch1", units="cm")
    assert res["ok"] is True, res
    assert _read(netlist)["board"] == {"outline": {"dxf": "board.dxf", "layer": "board v3_Sketch1",
                                                   "units": "cm"}}


def test_import_embed_mode_writes_the_loops_cutouts_and_datum(tmp_path):
    dxf = _t10_dxf(tmp_path / "t10.dxf")
    netlist = _netlist(tmp_path, {"w": 10, "h": 10, "corner_radius": 1, "auto_x": 2,
                                  "cutouts": [{"circle": [5, 5, 1]}], "datum": [1, 1]})
    res = mt.import_outline_dxf(str(netlist), str(dxf), mode="embed")
    assert res["ok"] is True, res
    board = _read(netlist)["board"]
    assert set(board) == {"w", "h", "outline", "cutouts", "datum", "auto_x"}
    assert board["auto_x"] == 2 and board["datum"] == [10.0, 30.0]
    assert (board["w"], board["h"]) == (60.0, 40.0) and len(board["cutouts"]) == 2
    assert {"circle": [30.0, 20.0, 2.5]} in board["cutouts"]
    assert res["source"] == "loop" and res["datum"] == [10.0, 30.0]
    assert any("cutouts" in n for n in res["notes"]) and any("datum" in n for n in res["notes"])
    assert ol.area(ol.from_board_block(board)) == pytest.approx(
        ol.area(ol.from_board_block(T10_BOARD)), abs=0.01)


def test_import_converts_fusion_parts_to_at_anchors(tmp_path):
    _t10_dxf(tmp_path / "mech" / "t10.dxf")
    netlist = _netlist(tmp_path)
    res = mt.import_outline_dxf(str(netlist), "mech/t10.dxf",
                                parts={"J1": [0, 0, 90], "U1": [5, 5, 0]})
    assert res["ok"] is True, res
    design = _read(netlist)
    j1, u1 = _part(design, "J1"), _part(design, "U1")
    assert j1["at"] == [10.0, 30.0, 90.0]                    # Fusion (0, 0) = the datum
    assert u1["at"] == [15.0, 25.0, 0.0]                     # (5, 5) -> (15, 25), Y up -> Y down
    assert not {"x", "y", "rot"} & set(j1)
    assert list(j1) == ["ref", "lib_id", "footprint", "at", "value"]   # 'at' where x was
    assert res["parts"] == {"J1": [10.0, 30.0, 90.0], "U1": [15.0, 25.0, 0.0]}
    assert _part(design, "R1") == COMPONENTS[2]


@pytest.mark.parametrize("form", ["file", "text"])
def test_import_reads_parts_from_a_json_file_or_text(form, tmp_path):
    _t10_dxf(tmp_path / "t10.dxf")
    netlist = _netlist(tmp_path)
    parts = {"J1": [-5, 5], "U1": [0, -5, 45]}               # [x, y] keeps the current rot
    if form == "file":
        (tmp_path / "parts.json").write_text(json.dumps(parts), encoding="utf-8")
        arg = "parts.json"
    else:
        arg = json.dumps(parts)
    res = mt.import_outline_dxf(str(netlist), "t10.dxf", mode="embed", parts=arg)
    assert res["ok"] is True, res
    design = _read(netlist)
    assert _part(design, "J1")["at"] == [5.0, 25.0, 90.0]
    assert _part(design, "U1")["at"] == [10.0, 35.0, 45.0]


@pytest.mark.parametrize("kwargs, why", [
    ({"parts": {"Q9": [0, 0, 0]}}, "Q9"),
    ({"parts": {"J1": [0, "x", 0]}}, "J1"),
    ({"parts": "[1, 2]"}, "object"),
    ({"parts": "nope.json"}, "nope.json"),
    ({"mode": "paste"}, "mode"),
    ({"dxf": "missing.dxf"}, "missing.dxf"),
    ({"units": "furlong"}, "furlong"),
    ({"layer": "NOPE"}, "NOPE"),
])
def test_import_errors_leave_the_netlist_unchanged(kwargs, why, tmp_path):
    _t10_dxf(tmp_path / "t10.dxf")
    netlist = _netlist(tmp_path)
    before = netlist.read_bytes()
    args = {"dxf": "t10.dxf", **kwargs}
    res = mt.import_outline_dxf(str(netlist), **args)
    assert res["ok"] is False and why in res["reason"], res
    assert netlist.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["netlist.json", "t10.dxf"]


def test_import_open_dxf_loop_is_refused_and_nothing_written(tmp_path):
    doc, msp = new_doc(4)
    msp.add_lwpolyline([(0, 0), (50, 0), (50, 30), (0, 30)], close=False)
    save(doc, tmp_path, "open.dxf")
    netlist = _netlist(tmp_path)
    before = netlist.read_bytes()
    res = mt.import_outline_dxf(str(netlist), "open.dxf")
    assert res["ok"] is False and "open.dxf" in res["reason"]
    assert netlist.read_bytes() == before and not (tmp_path / "netlist.json.bak").exists()


def test_import_without_backup_writes_no_bak(tmp_path):
    _t10_dxf(tmp_path / "t10.dxf")
    netlist = _netlist(tmp_path)
    res = mt.import_outline_dxf(str(netlist), "t10.dxf", backup=False)
    assert res["ok"] is True and res["backup"] is None
    assert not (tmp_path / "netlist.json.bak").exists()


def test_import_write_is_atomic(tmp_path, monkeypatch):
    _t10_dxf(tmp_path / "t10.dxf")
    netlist = _netlist(tmp_path)
    before = netlist.read_bytes()

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(mt.os, "replace", broken_replace)
    res = mt.import_outline_dxf(str(netlist), "t10.dxf", backup=False)
    assert res["ok"] is False and "disk full" in res["reason"]
    assert netlist.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["netlist.json", "t10.dxf"]


def test_import_refuses_a_netlist_that_is_not_a_json_object(tmp_path):
    _t10_dxf(tmp_path / "t10.dxf")
    netlist = tmp_path / "netlist.json"
    netlist.write_text("[1, 2", encoding="utf-8")
    res = mt.import_outline_dxf(str(netlist), "t10.dxf")
    assert res["ok"] is False and "JSON" in res["reason"]


# --- write_outline_dxf ----------------------------------------------------------------------
def test_write_outline_dxf_keeps_the_datum(tmp_path):
    netlist = _netlist(tmp_path, dict(T10_BOARD))
    res = mt.write_outline_dxf(str(netlist), "mech/out.dxf")
    assert res["ok"] is True, res
    assert Path(res["dxf"]) == tmp_path / "mech" / "out.dxf"
    back = dxf_io.read_board_outline(res["dxf"])
    assert back.datum == pytest.approx((10, 30)) and res["datum"] == [10.0, 30.0]
    assert mx.hausdorff(back.model, ol.from_board_block(T10_BOARD)) < 1e-6


def test_write_outline_dxf_defaults_the_datum_to_the_bbox_bottom_left(tmp_path):
    netlist = _netlist(tmp_path, {"w": 60, "h": 40, "corner_radius": 3})
    res = mt.write_outline_dxf(str(netlist), str(tmp_path / "rect.dxf"))
    assert res["ok"] is True and res["datum"] == [0.0, 40.0]
    assert "bottom-left" in res["datum_source"]
    assert dxf_io.read_board_outline(res["dxf"]).datum == pytest.approx((0, 40))


def test_write_outline_dxf_needs_a_board_block(tmp_path):
    netlist = _netlist(tmp_path, {})
    res = mt.write_outline_dxf(str(netlist), str(tmp_path / "o.dxf"))
    assert res["ok"] is False and "board" in res["reason"]


# --- export_outline_dxf ---------------------------------------------------------------------
def _dxf_writer(block: dict):
    return lambda out: dxf_io.write_outline_dxf(out, block)


def test_export_outline_dxf_runs_the_exact_command_and_matches_the_board(monkeypatch, t10_pcb):
    fake = _cli(monkeypatch, write=_dxf_writer(T10_BOARD))
    out = t10_pcb.with_name("edge.dxf")
    res = mt.export_outline_dxf(str(t10_pcb), str(out))
    assert res["ok"] is True, res
    assert fake.calls[0][0] == [CLI, "pcb", "export", "dxf", "--help"]
    assert fake.jobs == [[CLI, *DXF_CMD, str(out), str(t10_pcb)]]
    assert all("--use-contours" not in a and "--uc" not in a for a, _ in fake.calls)
    for _args, kw in fake.calls:
        assert kw["encoding"] == "utf-8" and kw["errors"] == "replace" and kw["timeout"] > 0
    assert res["hausdorff_mm"] <= 0.01 and res["dxf"] == str(out) and res["warnings"] == []


def test_export_outline_dxf_flags_a_datum_mismatch(monkeypatch, t10_pcb):
    _cli(monkeypatch, write=_dxf_writer({**T10_BOARD, "datum": [10.5, 30]}))
    res = mt.export_outline_dxf(str(t10_pcb), str(t10_pcb.with_name("edge.dxf")))
    assert res["ok"] is False and res["hausdorff_mm"] == pytest.approx(0.5, abs=1e-6)
    assert "0.01" in res["reason"]


def test_export_outline_dxf_drops_an_unsupported_flag_with_a_warning(monkeypatch, t10_pcb):
    help_text = DXF_HELP.replace("--mode-single", "--mode-other")
    fake = _cli(monkeypatch, write=_dxf_writer(T10_BOARD), help_text=help_text)
    res = mt.export_outline_dxf(str(t10_pcb), str(t10_pcb.with_name("edge.dxf")))
    assert res["ok"] is True, res
    assert "--mode-single" not in fake.jobs[0]
    assert any("--mode-single" in w for w in res["warnings"])


def test_export_outline_dxf_reads_back_by_header_units_without_ou(monkeypatch, t10_pcb):
    help_text = DXF_HELP.replace("--ou, --output-units", "--other-units")
    fake = _cli(monkeypatch, write=_dxf_writer(T10_BOARD), help_text=help_text)
    res = mt.export_outline_dxf(str(t10_pcb), str(t10_pcb.with_name("edge.dxf")))
    assert res["ok"] is True and "--ou" not in fake.jobs[0]
    assert any("--ou" in w for w in res["warnings"])


@pytest.mark.parametrize("fake_kw, why", [
    ({"output": "12:00:01: Error: cannot load board\n"}, "Error:"),
    ({"rc": 3}, "exited 3"),
    ({"write": None}, "no DXF"),
])
def test_export_outline_dxf_failures_are_not_ok(fake_kw, why, monkeypatch, t10_pcb):
    _cli(monkeypatch, **{"write": _dxf_writer(T10_BOARD), **fake_kw})
    res = mt.export_outline_dxf(str(t10_pcb), str(t10_pcb.with_name("edge.dxf")))
    assert res["ok"] is False and why in res["reason"], res


def test_export_outline_dxf_without_kicad_cli(monkeypatch, t10_pcb):
    monkeypatch.setattr(tc, "board_cli", lambda pcb="": None)
    res = mt.export_outline_dxf(str(t10_pcb), str(t10_pcb.with_name("edge.dxf")))
    assert res["ok"] is False and "kicad-cli" in res["reason"]


def test_export_outline_dxf_needs_edge_cuts_on_the_board(monkeypatch, tmp_path):
    _cli(monkeypatch, write=_dxf_writer(T10_BOARD))
    pcb = tmp_path / "blank.kicad_pcb"
    pcb.write_text('(kicad_pcb (version 20241229) (generator "pcbnew"))\n', encoding="utf-8")
    res = mt.export_outline_dxf(str(pcb), str(tmp_path / "edge.dxf"))
    assert res["ok"] is False and "Edge.Cuts" in res["reason"]


# --- export_step / export_pos_datum ---------------------------------------------------------
def _step(**over) -> str:
    vals = {"prefix": ".MILLI.", "x0": "-10.", "y0": "-10.", "x1": "50.", "y1": "30.",
            "z1": "1.6", **over}
    return TINY_STEP.format(**vals)


@pytest.mark.parametrize("board_only", [True, False])
def test_export_step_runs_the_exact_command_and_returns_the_bbox(board_only, monkeypatch,
                                                                 t10_pcb):
    fake = _cli(monkeypatch, write=lambda out: out.write_text(_step(), encoding="utf-8"),
                output="STEP file 'x.step' created.\n")
    out = t10_pcb.with_name("board.step")
    res = mt.export_step(str(t10_pcb), str(out), board_only=board_only)
    assert res["ok"] is True, res
    tail = ["--board-only"] if board_only else []
    assert fake.jobs == [[CLI, "pcb", "export", "step", "--drill-origin", "--force", *tail,
                          "-o", str(out), str(t10_pcb)]]
    assert res["bbox"]["x"] == [-10.0, 50.0] and res["bbox"]["y"] == [-10.0, 30.0]
    assert res["step"] == str(out)
    assert any("assembly" in w for w in res["warnings"]) is (not board_only)


def test_export_step_keeps_the_file_when_the_bbox_scan_fails(monkeypatch, t10_pcb):
    _cli(monkeypatch, write=lambda out: out.write_text("not a step file", encoding="utf-8"))
    res = mt.export_step(str(t10_pcb), str(t10_pcb.with_name("b.step")), board_only=True)
    assert res["ok"] is True and res["bbox"] is None
    assert any("bbox" in w for w in res["warnings"])


def test_export_step_failure_is_not_ok(monkeypatch, t10_pcb):
    _cli(monkeypatch, write=None, output="Error: no board outline\n", rc=1)
    res = mt.export_step(str(t10_pcb), str(t10_pcb.with_name("b.step")))
    assert res["ok"] is False and "Error:" in res["reason"]


def test_export_pos_datum_runs_the_exact_command(monkeypatch, t10_pcb):
    csv = "Ref,Val,Package,PosX,PosY,Rot,Side\n\"U1\",\"x\",\"SOIC\",5.0,5.0,0.0,top\n"
    fake = _cli(monkeypatch, write=lambda out: out.write_text(csv, encoding="utf-8"))
    out = t10_pcb.with_name("datum-pos.csv")
    res = mt.export_pos_datum(str(t10_pcb), str(out))
    assert res["ok"] is True, res
    assert fake.jobs == [[CLI, "pcb", "export", "pos", "--use-drill-file-origin", "--units", "mm",
                          "--format", "csv", "-o", str(out), str(t10_pcb)]]
    assert res["rows"] == 1 and res["csv"] == str(out)
    assert "export_fab_package" in res["note"] and "absolute" in res["note"]


def test_export_pos_datum_of_an_empty_board_has_no_rows(monkeypatch, t10_pcb):
    _cli(monkeypatch, write=lambda out: out.write_text("Ref,Val,Package,PosX,PosY,Rot,Side\n",
                                                       encoding="utf-8"))
    res = mt.export_pos_datum(str(t10_pcb), str(t10_pcb.with_name("p.csv")))
    assert res["ok"] is True and res["rows"] == 0


# --- mech_export.step_bbox ------------------------------------------------------------------
def test_step_bbox_uses_the_brep_vertices_not_every_point(tmp_path):
    path = tmp_path / "tiny.step"
    path.write_text(_step(), encoding="utf-8")
    box = mx.step_bbox(path)
    assert box["x"] == [-10.0, 50.0] and box["y"] == [-10.0, 30.0] and box["z"] == [0.0, 1.6]
    assert box["vertices"] == 2 and box["points"] == 5 and box["unit"] == "mm"


def test_step_bbox_scales_metres_to_mm(tmp_path):
    path = tmp_path / "metres.step"
    path.write_text(_step(prefix="$", x0="-0.01", y0="-0.01", x1="0.05", y1="0.03", z1="0.0016"),
                    encoding="utf-8")
    box = mx.step_bbox(path)
    assert box["x"] == pytest.approx([-10, 50]) and box["z"] == pytest.approx([0, 1.6])
    assert box["scale"] == 1000.0


ARC_STEP = """ISO-10303-21;
DATA;
#1 = ( LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.) );
#10 = CARTESIAN_POINT('',(0.,0.,0.));
#11 = DIRECTION('',(0.,0.,1.));
#12 = DIRECTION('',(1.,0.,0.));
#13 = AXIS2_PLACEMENT_3D('',#10,#11,#12);
#14 = CIRCLE('',#13,5.);
#20 = CARTESIAN_POINT('',(5.,0.,0.));
#21 = VERTEX_POINT('',#20);
#22 = CARTESIAN_POINT('',(-5.,0.,0.));
#23 = VERTEX_POINT('',#22);
#30 = EDGE_CURVE('',#21,#23,#14,{sense});
ENDSEC;
END-ISO-10303-21;
"""


@pytest.mark.parametrize("sense, y", [(".T.", [0.0, 5.0]), (".F.", [-5.0, 0.0])])
def test_step_bbox_includes_the_arc_extremes_between_vertices(sense, y, tmp_path):
    path = tmp_path / "arc.step"
    path.write_text(ARC_STEP.format(sense=sense), encoding="utf-8")
    box = mx.step_bbox(path)
    assert box["x"] == pytest.approx([-5, 5]) and box["y"] == pytest.approx(y, abs=1e-9)
    assert box["arcs"] == 1


def test_step_bbox_skips_a_circle_edge_with_unresolved_references(tmp_path):
    path = tmp_path / "broken.step"
    path.write_text(ARC_STEP.format(sense=".T.").replace("CIRCLE('',#13,", "CIRCLE('',#99,"),
                    encoding="utf-8")
    box = mx.step_bbox(path)
    assert box["arcs"] == 0 and box["x"] == [-5.0, 5.0] and box["y"] == [0.0, 0.0]


def test_step_bbox_of_a_full_circle_edge(tmp_path):
    text = ARC_STEP.format(sense=".T.").replace("#30 = EDGE_CURVE('',#21,#23,",
                                                "#30 = EDGE_CURVE('',#21,#21,")
    path = tmp_path / "circle.step"
    path.write_text(text, encoding="utf-8")
    box = mx.step_bbox(path)
    assert box["x"] == pytest.approx([-5, 5]) and box["y"] == pytest.approx([-5, 5])


@pytest.mark.parametrize("text", ["hello", "ISO-10303-21;\nDATA;\nENDSEC;\n"])
def test_step_bbox_refuses_a_file_without_points(text, tmp_path):
    path = tmp_path / "bad.step"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(mx.StepScanError):
        mx.step_bbox(path)


# --- mech_export helpers --------------------------------------------------------------------
def test_dxf_command_never_uses_contours():
    args, warnings, mm = mx.dxf_command(CLI, "b.kicad_pcb", "o.dxf", DXF_HELP)
    assert args == [CLI, *DXF_CMD, "o.dxf", "b.kicad_pcb"] and warnings == [] and mm is True
    args, warnings, _mm = mx.dxf_command(CLI, "b.kicad_pcb", "o.dxf", "")
    assert args[:-3] == [CLI, *DXF_CMD[:-1]] and warnings      # unreadable help: keep every flag


def test_hausdorff_is_symmetric_and_exact_on_offsets():
    a = ol.from_board_block({"w": 60, "h": 40})
    b = ol.translate(a, 0.3, 0.0)
    assert mx.hausdorff(a, a) == pytest.approx(0.0, abs=1e-12)
    assert mx.hausdorff(a, b) == pytest.approx(0.3, abs=1e-9) == mx.hausdorff(b, a)
    t10 = ol.from_board_block(T10_BOARD)
    no_hole = ol.Outline(t10.outer, t10.cutouts[:1], t10.datum)
    # the missing hole's far rim (30, 22.5) is 8.5 mm from the stadium's bottom (30, 14)
    assert mx.hausdorff(t10, no_hole) == pytest.approx(8.5, abs=0.01)
    assert mx.hausdorff(no_hole, t10) == pytest.approx(8.5, abs=0.01)


def test_board_file_outline_reads_edge_cuts_and_the_aux_origin(t10_pcb):
    o = mx.board_file_outline(t10_pcb)
    assert o.datum == pytest.approx((10, 30))
    assert mx.hausdorff(o, ol.from_board_block(T10_BOARD)) < 1e-6


# --- validate_board_outline -----------------------------------------------------------------
def _codes(issues: list[dict]) -> list[str]:
    return [i["code"] for i in issues]


def test_validate_a_clean_t10_board(tmp_path):
    netlist = _netlist(tmp_path, dict(T10_BOARD),
                       components=[{"ref": "U1", "lib_id": "a:b", "footprint": "a:b",
                                    "at": [15, 20, 0]}])
    res = mt.validate_board_outline(str(netlist))
    assert res["ok"] is True and res["errors"] == [], res
    assert res["source"] == "loop" and res["summary"]["bbox"] == [0, 0, 60, 40]
    assert res["summary"]["datum"] == [10, 30] and res["fab_profile"] == "JLCPCB"


@pytest.mark.parametrize("part, code, severity", [
    ({"at": [30, 20, 0]}, "part_in_cutout", "error"),
    ({"x": 70, "y": 20}, "part_outside", "error"),
    ({"at": [0.5, 20, 0]}, "part_near_edge", "warning"),
    ({"at": [-1, 20, 0], "overhang": 2.0}, None, None),
    ({"at": [-3, 20, 0], "overhang": 2.0}, "part_outside", "error"),
])
def test_validate_checks_fixed_parts_against_the_outline(part, code, severity, tmp_path):
    comp = {"ref": "J9", "lib_id": "a:b", "footprint": "a:b", **part}
    netlist = _netlist(tmp_path, dict(T10_BOARD), components=[comp])
    res = mt.validate_board_outline(str(netlist))
    found = [i for i in res["errors"] + res["warnings"] if i["ref"] == "J9"]
    if code is None:
        assert found == [] and res["ok"] is True
    else:
        assert [(i["code"], i["severity"]) for i in found] == [(code, severity)]
        assert "board frame" in found[0]["message"]


def test_validate_default_corner_holes_need_positions_on_a_shaped_outline(tmp_path):
    netlist = _netlist(tmp_path, dict(T10_BOARD), mounting_holes=True)
    res = mt.validate_board_outline(str(netlist))
    assert res["ok"] is False and "mounting_holes_positions" in _codes(res["errors"])


def test_validate_mounting_holes_use_the_drill_circle(tmp_path):
    holes = {"footprint": "MountingHole:MountingHole_2.2mm_M2", "positions": [[5, 20], [1.2, 20]]}
    netlist = _netlist(tmp_path, dict(T10_BOARD), components=[], mounting_holes=holes)
    res = mt.validate_board_outline(str(netlist))
    issues = res["errors"] + res["warnings"]
    assert _codes(issues) == ["hole_edge"] and "2.2" in issues[0]["message"], issues


def test_validate_rect_board_default_corner_holes_are_fine(tmp_path):
    netlist = _netlist(tmp_path, {"w": 60, "h": 40}, mounting_holes=True)
    res = mt.validate_board_outline(str(netlist))
    assert res["ok"] is True and res["source"] == "rect", res


def test_validate_reports_an_unresolvable_outline(tmp_path):
    netlist = _netlist(tmp_path, {"w": 10, "h": 10, "corner_radius": 6})
    res = mt.validate_board_outline(str(netlist))
    assert res["ok"] is False and _codes(res["errors"]) == ["outline_unresolved"]
    assert "corner_radius" in res["errors"][0]["message"]


def test_validate_reports_fab_issues_and_dxf_warnings(tmp_path):
    doc, msp = fusion_doc()
    sk = Sketch(msp, k=10.0)
    fusion_board(sk)
    sk.slot(35, 40, -20, 0.3)                                # a 0.6 mm slot: too narrow to mill
    sk.circle((-5, -20), 0.4)                                # a 0.8 mm circle: a drill warning
    _save(doc, tmp_path / "mech", "o.dxf")
    netlist = _netlist(tmp_path, {"outline": {"dxf": "mech/o.dxf"}}, components=[])
    res = mt.validate_board_outline(str(netlist))
    assert res["source"] == "dxf"
    assert "fab_slot_width" in _codes(res["errors"] + res["warnings"])
    assert "dxf_warning" in _codes(res["warnings"])           # the drill-sized circle


def test_validate_without_a_board_block_is_ok_with_a_warning(tmp_path):
    netlist = _netlist(tmp_path, {})
    res = mt.validate_board_outline(str(netlist))
    assert res["ok"] is True and res["source"] == "none"
    assert _codes(res["warnings"]) == ["no_board_outline"]
