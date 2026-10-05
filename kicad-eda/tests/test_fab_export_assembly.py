"""F46-21: export_fab_package writes the JLCPCB pair through assembly_files.

board_fab.export (the body of board_pipeline_tools.export_fab_package) hands
the netlist to server/tools/assembly_files: parts flagged ``dnp: true``,
``in_bom: false`` or ``board_only: true`` are left out of BOM.csv and CPL.csv
and reported as ``bom_excluded`` {ref: reason}; pos / design disagreements are
``assembly_warnings``. The kicad-cli pos export is unchanged (the filtering is
ours, so it also holds for footprints without the DNP attribute).

kicad-cli is faked: tc.board_cli names a fake binary and tc.run writes empty
Gerbers, an empty drill file and a kicad-cli style pos CSV. GOLDEN_BOM /
GOLDEN_CPL are the bytes the pre-F46-21 writer (board_fab.write_assembly_csvs
as X-09 split it out) produced for GOLDEN_DESIGN with GOLDEN_POS, captured
before the edit: a design without the flags must keep producing them.
"""
from __future__ import annotations

import ast
import csv
import io
import json
import os
import subprocess
import sys
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

import assembly_files
import board_fab
import board_pipeline_tools as bpt
import toolchain as tc

SERVER = Path(__file__).resolve().parents[1] / "server"
FAKE_CLI = "kicad-cli-fake"
POS_HEADER = "Ref,Val,Package,PosX,PosY,Rot,Side"
GERBER_LAYERS = ("F_Cu", "In1_Cu", "In2_Cu", "B_Cu", "F_Mask", "B_Mask", "F_Silkscreen",
                 "B_Silkscreen", "F_Paste", "B_Paste", "Edge_Cuts")
FakeRun = Callable[..., subprocess.CompletedProcess]


# ----------------------------------------------------------------------------
# designs and pos files
# ----------------------------------------------------------------------------
def _comp(ref: str, value: str = "10k", footprint: str = "Resistor_SMD:R_0603_1608Metric",
          **extra: object) -> dict:
    return {"ref": ref, "lib_id": "Device:R", "value": value, "footprint": footprint, **extra}


def _pos_line(ref: str, val: str, package: str, x: float, y: float, rot: float,
              side: str = "top") -> str:
    """One row as `kicad-cli pcb export pos --format csv` writes it (texts
    quoted with quotes doubled, numbers with 6 decimals, side bare)."""
    def q(text: str) -> str:
        return '"' + text.replace('"', '""') + '"'
    return f"{q(ref)},{q(val)},{q(package)},{x:.6f},{y:.6f},{rot:.6f},{side}"


def _pos_bytes(lines: Sequence[str]) -> bytes:
    """The pos file kicad-cli 9 writes: header first, \\r\\n rows, UTF-8."""
    return "".join(f"{line}\r\n" for line in (POS_HEADER, *lines)).encode("utf-8")


# No dnp / in_bom false / board_only part (L1 carries the flags at their
# assembled values); every group's designators sort the same naturally and
# lexicographically, which is the only BOM difference to the legacy writer.
GOLDEN_DESIGN: dict = {"name": "golden", "nets": {}, "components": [
    _comp("R3", lcsc="C25804", mpn="0603WAF1002T5E"),
    _comp("R1", lcsc="C25804", mpn="0603WAF1002T5E"),
    _comp("R2", "4k7", lcsc="C23162"),
    _comp("C2", "4.7µF", "Capacitor_SMD:C_0805_2012Metric", lcsc="C1779"),
    _comp("C1", "4.7µF", "Capacitor_SMD:C_0805_2012Metric", lcsc="C1779"),
    _comp("C3", "100nF", "Capacitor_SMD:C_0402_1005Metric", side="B"),
    _comp("U1", "ESP32-C3-WROOM-02", "RF_Module:ESP32-C3-WROOM-02", mpn="ESP32-C3-WROOM-02-N4"),
    _comp("J1", 'Conn, 2-pin "JST"', "Connector_JST:JST_PH_B2B-PH-K_1x02_P2.00mm_Vertical"),
    _comp("L1", "2.2µH ±20%", "Inductor_SMD:L_1210_3225Metric",
          dnp=False, in_bom=True, board_only=False),
    _comp("TP1", "TP", "TestPoint_Pad_D1.0mm"),
]}
GOLDEN_POS: tuple[str, ...] = (
    _pos_line("C1", "4.7µF", "C_0805_2012Metric", 113.5, -84.25, 90),
    _pos_line("C2", "4.7µF", "C_0805_2012Metric", 116.0, -84.25, 90),
    _pos_line("C3", "100nF", "C_0402_1005Metric", 120.125, -90.0, 180, "bottom"),
    _pos_line("J1", 'Conn, 2-pin "JST"', "JST_PH_B2B-PH-K_1x02_P2.00mm_Vertical",
              100.0, -100.0, -90),
    _pos_line("L1", "2.2µH ±20%", "L_1210_3225Metric", 130.0, -80.5, 0),
    _pos_line("R1", "10k", "R_0603_1608Metric", 110.0, -95.0, 0),
    _pos_line("R2", "4k7", "R_0603_1608Metric", 111.6, -95.0, 270),
    _pos_line("R3", "10k", "R_0603_1608Metric", 113.2, -95.0, 0),
    _pos_line("TP1", "TP", "TestPoint_Pad_D1.0mm", 140.0, -70.0, 0),
    _pos_line("U1", "ESP32-C3-WROOM-02", "ESP32-C3-WROOM-02", 125.0, -110.0, 0),
)
# Captured from the pre-F46-21 board_fab.export (legacy writer, board_fab.py
# sha256 cb607a3f...) on GOLDEN_DESIGN + GOLDEN_POS; csv \r\n rows, UTF-8, no BOM.
GOLDEN_BOM = (
    "Comment,Designator,Footprint,LCSC Part #,MPN\r\n"
    "100nF,C3,C_0402_1005Metric,,\r\n"
    '10k,"R1,R3",R_0603_1608Metric,C25804,0603WAF1002T5E\r\n'
    "2.2µH ±20%,L1,L_1210_3225Metric,,\r\n"
    '4.7µF,"C1,C2",C_0805_2012Metric,C1779,\r\n'
    "4k7,R2,R_0603_1608Metric,C23162,\r\n"
    '"Conn, 2-pin ""JST""",J1,JST_PH_B2B-PH-K_1x02_P2.00mm_Vertical,,\r\n'
    "ESP32-C3-WROOM-02,U1,ESP32-C3-WROOM-02,,ESP32-C3-WROOM-02-N4\r\n"
    "TP,TP1,TestPoint_Pad_D1.0mm,,\r\n"
)
GOLDEN_CPL = (
    "Designator,Mid X,Mid Y,Layer,Rotation\r\n"
    "C1,113.500000,-84.250000,top,90.000000\r\n"
    "C2,116.000000,-84.250000,top,90.000000\r\n"
    "C3,120.125000,-90.000000,bottom,180.000000\r\n"
    "J1,100.000000,-100.000000,top,-90.000000\r\n"
    "L1,130.000000,-80.500000,top,0.000000\r\n"
    "R1,110.000000,-95.000000,top,0.000000\r\n"
    "R2,111.600000,-95.000000,top,270.000000\r\n"
    "R3,113.200000,-95.000000,top,0.000000\r\n"
    "TP1,140.000000,-70.000000,top,0.000000\r\n"
    "U1,125.000000,-110.000000,top,0.000000\r\n"
)


# ----------------------------------------------------------------------------
# fake kicad-cli
# ----------------------------------------------------------------------------
def _fake_cli(monkeypatch: pytest.MonkeyPatch, pos: Sequence[str] | None,
              stderr: str = "") -> list[list[str]]:
    """Patch tc.board_cli / tc.run with a kicad-cli that writes empty Gerbers
    and drill files and, unless `pos` is None, a pos CSV of those rows.
    Returns the argv list of every call."""
    calls: list[list[str]] = []

    def run(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        calls.append(list(args))
        target, stem = Path(args[args.index("-o") + 1]), Path(args[-1]).stem
        job = args[3]
        if job == "gerbers":
            target.mkdir(parents=True, exist_ok=True)
            for layer in GERBER_LAYERS:
                (target / f"{stem}-{layer}.gbr").write_bytes(b"")
            (target / f"{stem}-job.gbrjob").write_bytes(b"")
        elif job == "drill":
            (target / f"{stem}.drl").write_bytes(b"")
        elif job == "pos" and pos is not None:
            target.write_bytes(_pos_bytes(pos))
        return subprocess.CompletedProcess(args, 0, "", stderr)

    monkeypatch.setattr(tc, "board_cli", lambda pcb="": FAKE_CLI)
    monkeypatch.setattr(tc, "run", run)
    return calls


def _netlist(tmp_path: Path, design: dict) -> str:
    path = tmp_path / "netlist.json"
    path.write_text(json.dumps(design, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _export(tmp_path: Path, netlist: str = "") -> dict:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20241229))\n", encoding="utf-8")
    return bpt.export_fab_package(str(pcb), str(tmp_path / "fab"), str(tmp_path / "fab.zip"),
                                  netlist=netlist)


def _zipped(res: dict, name: str) -> bytes:
    with zipfile.ZipFile(res["zip"]) as z:
        return z.read(name)


def _rows(data: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(data.decode("utf-8"), newline="")))


# ----------------------------------------------------------------------------
# flagged parts
# ----------------------------------------------------------------------------
FLAGGED_DESIGN: dict = {"name": "flagged", "nets": {}, "components": [
    _comp("R1", lcsc="C25804"),
    _comp("R10", lcsc="C25804"),
    _comp("R5", lcsc="C25804", dnp=True),
    _comp("C4", "100nF", "Capacitor_SMD:C_0402_1005Metric", in_bom=False),
    _comp("C1", "100nF", "Capacitor_SMD:C_0402_1005Metric"),
    _comp("H1", "MountingHole", "MountingHole:MountingHole_3.2mm_M3", board_only=True),
    _comp("FID1", "Fiducial", "Fiducial:Fiducial_1mm_Mask2mm", dnp=True, board_only=True),
]}
# kicad-cli places every footprint, flagged or not (no --exclude-dnp)
FLAGGED_POS: tuple[str, ...] = (
    _pos_line("C1", "100nF", "C_0402_1005Metric", 10.0, -10.0, 0),
    _pos_line("C4", "100nF", "C_0402_1005Metric", 12.0, -10.0, 0),
    _pos_line("FID1", "Fiducial", "Fiducial_1mm_Mask2mm", 2.0, -2.0, 0),
    _pos_line("H1", "MountingHole", "MountingHole_3.2mm_M3", 3.5, -3.5, 0),
    _pos_line("R1", "10k", "R_0603_1608Metric", 20.0, -10.0, 90),
    _pos_line("R5", "10k", "R_0603_1608Metric", 22.0, -10.0, 90),
    _pos_line("R10", "10k", "R_0603_1608Metric", 24.0, -10.0, 90, "bottom"),
)
FLAGGED_EXCLUDED = {"C4": "in_bom false", "FID1": "dnp, board_only", "H1": "board_only",
                    "R5": "dnp"}


def test_flagged_parts_are_left_out_of_bom_and_cpl(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_cli(monkeypatch, FLAGGED_POS)
    res = _export(tmp_path, _netlist(tmp_path, FLAGGED_DESIGN))
    assert (res["ok"], res["bom"], res["cpl"]) == (True, True, True)
    assert res["bom_excluded"] == FLAGGED_EXCLUDED
    assert list(res["bom_excluded"]) == ["C4", "FID1", "H1", "R5"]      # natural order
    assert res["assembly_warnings"] == []
    bom = (tmp_path / "fab" / "BOM.csv").read_bytes()
    cpl = (tmp_path / "fab" / "CPL.csv").read_bytes()
    assert _rows(bom) == [
        ["Comment", "Designator", "Footprint", "LCSC Part #", "MPN"],
        ["100nF", "C1", "C_0402_1005Metric", "", ""],
        ["10k", "R1,R10", "R_0603_1608Metric", "C25804", ""]]
    assert _rows(cpl) == [
        ["Designator", "Mid X", "Mid Y", "Layer", "Rotation"],
        ["C1", "10.000000", "-10.000000", "top", "0.000000"],
        ["R1", "20.000000", "-10.000000", "top", "90.000000"],
        ["R10", "24.000000", "-10.000000", "bottom", "90.000000"]]
    designators = {ref for row in _rows(bom)[1:] for ref in row[1].split(",")}
    placed = {row[0] for row in _rows(cpl)[1:]}
    assert not set(FLAGGED_EXCLUDED) & (designators | placed)         # in neither file
    assert _zipped(res, "BOM.csv") == bom and _zipped(res, "CPL.csv") == cpl
    assert {"BOM.csv", "CPL.csv", "board-pos.csv"} <= set(res["files"])
    json.dumps(res)                                                    # MCP result


def test_bom_excluded_is_empty_for_a_design_without_flags(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_cli(monkeypatch, GOLDEN_POS)
    res = _export(tmp_path, _netlist(tmp_path, GOLDEN_DESIGN))
    assert (res["bom_excluded"], res["assembly_warnings"]) == ({}, [])


def test_designators_are_natural_sorted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # the one deliberate BOM change: the legacy writer sorted R10 before R2
    design = {"components": [_comp("R10"), _comp("R2"), _comp("R1")]}
    _fake_cli(monkeypatch, [_pos_line(r, "10k", "R_0603_1608Metric", 1, 1, 0)
                            for r in ("R1", "R2", "R10")])
    _export(tmp_path, _netlist(tmp_path, design))
    assert _rows((tmp_path / "fab" / "BOM.csv").read_bytes())[1][1] == "R1,R2,R10"


def test_pos_disagreements_are_assembly_warnings(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    # a logo footprint the netlist does not know, and a part kicad-cli did not place
    design = {"components": [_comp("R1"), _comp("R2")]}
    _fake_cli(monkeypatch, [_pos_line("R1", "10k", "R_0603_1608Metric", 1, 1, 0),
                            _pos_line("G1", "LOGO", "Logo", 5, 5, 0)])
    res = _export(tmp_path, _netlist(tmp_path, design))
    assert res["assembly_warnings"] == [
        "pos ref G1 is not in the design; no CPL row",
        "BOM part R2 has no placement in the pos file; no CPL row"]
    assert [r[0] for r in _rows((tmp_path / "fab" / "CPL.csv").read_bytes())[1:]] == ["R1"]


# ----------------------------------------------------------------------------
# golden: an unflagged design's bytes are the pre-F46-21 writer's
# ----------------------------------------------------------------------------
def test_unflagged_design_matches_the_pre_edit_golden(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_cli(monkeypatch, GOLDEN_POS)
    res = _export(tmp_path, _netlist(tmp_path, GOLDEN_DESIGN))
    assert (res["ok"], res["bom"], res["cpl"]) == (True, True, True)
    assert (tmp_path / "fab" / "BOM.csv").read_bytes() == GOLDEN_BOM.encode("utf-8")
    assert (tmp_path / "fab" / "CPL.csv").read_bytes() == GOLDEN_CPL.encode("utf-8")
    assert _zipped(res, "BOM.csv") == GOLDEN_BOM.encode("utf-8")
    assert _zipped(res, "CPL.csv") == GOLDEN_CPL.encode("utf-8")


def test_golden_covers_the_shapes_it_must(tmp_path: Path) -> None:
    rows = _rows(GOLDEN_BOM.encode("utf-8"))
    assert len(rows) == 9 and len(_rows(GOLDEN_CPL.encode("utf-8"))) == 1 + len(GOLDEN_POS)
    assert '"Conn, 2-pin ""JST"""' in GOLDEN_BOM                        # quoting
    assert "4.7µF" in GOLDEN_BOM and "C1,C2" in GOLDEN_BOM             # 'µ', grouping
    assert "0603WAF1002T5E" in GOLDEN_BOM and "bottom" in GOLDEN_CPL    # MPN, B side
    assert not any(c.get("dnp") is True or c.get("in_bom") is False or c.get("board_only")
                   for c in GOLDEN_DESIGN["components"])


def test_missing_pos_file_writes_the_golden_bom_only(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_cli(monkeypatch, None)                      # kicad-cli wrote no pos file
    res = _export(tmp_path, _netlist(tmp_path, GOLDEN_DESIGN))
    assert (res["bom"], res["cpl"]) == (True, False)
    assert (tmp_path / "fab" / "BOM.csv").read_bytes() == GOLDEN_BOM.encode("utf-8")
    assert not (tmp_path / "fab" / "CPL.csv").exists()
    assert len(res["assembly_warnings"]) == 1
    assert res["assembly_warnings"][0].startswith("no pick-and-place file ")


# ----------------------------------------------------------------------------
# no netlist / a bad netlist
# ----------------------------------------------------------------------------
def test_no_netlist_writes_no_assembly_pair(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_cli(monkeypatch, GOLDEN_POS)
    res = _export(tmp_path)
    assert (res["ok"], res["bom"], res["cpl"]) == (True, False, False)
    assert (res["bom_excluded"], res["assembly_warnings"]) == ({}, [])
    assert not (tmp_path / "fab" / "BOM.csv").exists()
    assert [c[3] for c in calls] == ["gerbers", "drill", "pos"]


def test_a_missing_netlist_file_is_reported(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_cli(monkeypatch, GOLDEN_POS)
    missing = str(tmp_path / "nope" / "netlist.json")
    res = _export(tmp_path, missing)
    assert (res["ok"], res["bom"], res["cpl"], res["bom_excluded"]) == (True, False, False, {})
    assert res["assembly_warnings"] == [
        f"netlist {missing} not found; BOM.csv / CPL.csv not written"]
    assert not (tmp_path / "fab" / "BOM.csv").exists()


@pytest.mark.parametrize("content, kind, message", [
    ("{not json", "unreadable", "Expecting property name"),
    (b"\xff\xfe{}", "unreadable", "codec can't decode"),
    ("[]", "malformed", "design must be a JSON object"),
    ('{"components": [{"value": "10k"}]}', "malformed", "component #0 has no 'ref'"),
], ids=["not-json", "not-utf8", "not-an-object", "ref-less-part"])
def test_a_bad_netlist_is_reported_and_the_gerbers_still_ship(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | bytes, kind: str,
        message: str) -> None:
    _fake_cli(monkeypatch, GOLDEN_POS)
    path = tmp_path / "netlist.json"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    res = _export(tmp_path, str(path))
    assert (res["ok"], res["bom"], res["cpl"], res["bom_excluded"]) == (True, False, False, {})
    [warning] = res["assembly_warnings"]
    assert warning.startswith(f"netlist {path} {kind}: ") and message in warning
    assert warning.endswith("; BOM.csv / CPL.csv not written")
    assert not (tmp_path / "fab" / "BOM.csv").exists()
    assert "board-F_Cu.gbr" in res["files"]


# ----------------------------------------------------------------------------
# what stays as it was
# ----------------------------------------------------------------------------
def test_the_kicad_cli_jobs_are_unchanged(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_cli(monkeypatch, FLAGGED_POS, stderr="warn\n")
    res = _export(tmp_path, _netlist(tmp_path, FLAGGED_DESIGN))
    pcb, out = str(tmp_path / "board.kicad_pcb"), str(tmp_path / "fab")
    assert calls == [
        [FAKE_CLI, "pcb", "export", "gerbers", "-o", out, pcb],
        [FAKE_CLI, "pcb", "export", "drill", "-o", out, pcb],
        [FAKE_CLI, "pcb", "export", "pos", "-o", str(Path(out) / "board-pos.csv"),
         "--format", "csv", "--units", "mm", pcb]]                     # no --exclude-dnp
    assert set(res) == {"ok", "out_dir", "zip", "fab_profile", "kicad_cli", "bom", "cpl",
                        "bom_excluded", "assembly_warnings", "files", "stderr"}
    assert (res["out_dir"], res["zip"], res["kicad_cli"]) == (out, str(tmp_path / "fab.zip"),
                                                              FAKE_CLI)
    assert res["fab_profile"] == tc.FAB_PROFILE and res["stderr"] == "warn\n" * 3
    # kicad-cli's own pos file still ships unfiltered next to the filtered CPL
    assert b'"R5"' in _zipped(res, "board-pos.csv")


def test_no_kicad_cli_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tc, "board_cli", lambda pcb="": None)
    monkeypatch.setattr(tc, "run", lambda *a, **kw: pytest.fail("kicad-cli was run"))
    res = _export(tmp_path, _netlist(tmp_path, FLAGGED_DESIGN))
    assert res == {"ok": False, "reason": "kicad-cli not found; install KiCad."}
    assert not (tmp_path / "fab").exists()


def test_export_delegates_the_pair_to_assembly_files(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_cli(monkeypatch, FLAGGED_POS)
    real = assembly_files.write_assembly_csvs
    seen: list[tuple[Any, ...]] = []

    def spy(design: dict, out_dir: str, pos_csv: str) -> dict:
        seen.append((design, out_dir, pos_csv))
        return real(design, out_dir, pos_csv)

    monkeypatch.setattr(assembly_files, "write_assembly_csvs", spy)
    _export(tmp_path, _netlist(tmp_path, FLAGGED_DESIGN))
    out = str(tmp_path / "fab")
    assert seen == [(FLAGGED_DESIGN, out, str(Path(out) / "board-pos.csv"))]


def test_write_assembly_csvs_returns_the_assembly_files_summary(tmp_path: Path) -> None:
    pos = tmp_path / "b-pos.csv"
    pos.write_bytes(_pos_bytes(FLAGGED_POS))
    res = board_fab.write_assembly_csvs(_netlist(tmp_path, FLAGGED_DESIGN), str(tmp_path),
                                        str(pos))
    assert res == {"bom": True, "cpl": True, "bom_lines": 2, "cpl_rows": 3,
                   "excluded": FLAGGED_EXCLUDED, "warnings": []}


def test_a_failure_to_write_the_pair_raises(tmp_path: Path) -> None:
    # not a netlist problem: it must not come back as an "unreadable" warning
    blocker = tmp_path / "fab"
    blocker.write_text("a file where the output directory should be", encoding="utf-8")
    with pytest.raises(OSError):
        board_fab.write_assembly_csvs(_netlist(tmp_path, FLAGGED_DESIGN), str(blocker), "")


def test_board_fab_has_no_csv_writer_of_its_own() -> None:
    # the legacy writer is gone: BOM / CPL bytes come from assembly_files only
    tree = ast.parse(Path(board_fab.__file__).read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}
    assert "csv" not in imported


def test_assembly_files_is_found_without_server_tools_on_sys_path(tmp_path: Path) -> None:
    # the MCP server imports board_fab with server/ on sys.path only
    netlist = _netlist(tmp_path, {"components": [_comp("R1"), _comp("R2", dnp=True)]})
    code = ("import json, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import board_fab\n"
            "print(json.dumps(board_fab.write_assembly_csvs(sys.argv[2], sys.argv[3], '')))\n")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run([sys.executable, "-c", code, str(SERVER), netlist, str(tmp_path)],
                          cwd=tmp_path, env=env, capture_output=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr[-1500:]
    res = json.loads(proc.stdout.strip().splitlines()[-1])
    assert (res["bom"], res["cpl"], res["excluded"]) == (True, False, {"R2": "dnp"})
    assert (tmp_path / "BOM.csv").read_text(encoding="utf-8").count("\n") == 2
