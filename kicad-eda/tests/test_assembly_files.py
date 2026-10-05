"""F46-06: assembly_files — JLCPCB BOM.csv / CPL.csv honouring the D4 flags.

The legacy writer (board_fab.write_assembly_csvs since X-09, before that
board_pipeline_tools._write_assembly_csvs) put every component in the BOM and
copied every kicad-cli pos row into the CPL. assembly_files leaves out parts
with ``dnp: true``, ``in_bom: false`` or ``board_only: true`` (with a reason),
natural-sorts designators and warns about pos / design disagreements; for a
design without those flags its bytes equal the legacy writer's. The golden is
produced in the test by the shipped legacy writer while it exists and by a
verbatim frozen copy of it (F46-21 rewires the shipped one onto this module;
the shipped case then skips with the reason, it never fails).
"""
from __future__ import annotations

import ast
import copy
import csv
import inspect
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

import assembly_files as af
import board_fab
from conftest import load_example

TOOLS = Path(__file__).resolve().parents[1] / "server" / "tools"
POS_HEADER = "Ref,Val,Package,PosX,PosY,Rot,Side"
LegacyWriter = Callable[[str, str, str], tuple[bool, bool]]
SHIPPED_SKIP = "the frozen copy guards the golden"


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def _comp(ref: str, value: str = "10k", footprint: str = "Resistor_SMD:R_0603_1608Metric",
          **extra: object) -> dict:
    return {"ref": ref, "lib_id": "Device:R", "value": value, "footprint": footprint, **extra}


def _design(*components: dict) -> dict:
    return {"name": "t", "components": list(components), "nets": {}}


def _pos_line(ref: str, val: str, package: str, x: float, y: float, rot: float,
              side: str) -> str:
    """One row as kicad-cli 9 `pcb export pos --format csv` writes it (texts
    quoted, numbers with 6 decimals, side bare); quotes in a text are doubled."""
    def q(text: str) -> str:
        return '"' + text.replace('"', '""') + '"'
    return f"{q(ref)},{q(val)},{q(package)},{x:.6f},{y:.6f},{rot:.6f},{side}"


def _write_pos(path: Path, lines: list[str], *, bom: bool = False) -> Path:
    text = "\r\n".join([POS_HEADER, *lines]) + "\r\n"
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    return path


def _pos_for(design: dict, skip: tuple[str, ...] = ()) -> list[str]:
    """A kicad-cli style pos row for every component, in file (ref) order."""
    lines = []
    for i, c in enumerate(design["components"]):
        if c["ref"] in skip:
            continue
        side = "bottom" if c.get("side") in ("B", "b") else "top"
        lines.append(_pos_line(c["ref"], c.get("value", ""), c["footprint"].split(":")[-1],
                               10.0 + 1.25 * i, -20.5 - 0.5 * i, (90.0 * i) % 360, side))
    return lines


def _read_csv(path: Path) -> list[list[str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def _frozen_legacy(netlist: str, out: str, pos: str) -> tuple[bool, bool]:
    """Verbatim copy of board_fab.write_assembly_csvs as X-09 moved it out of
    board_pipeline_tools (_write_assembly_csvs at 6a8f94d); frozen here so the
    golden survives F46-21 rewiring the shipped writer onto assembly_files."""
    design = json.loads(Path(netlist).read_text(encoding="utf-8"))
    groups: dict[tuple, list] = {}
    for c in design.get("components", []):
        key = (c.get("value", ""), c.get("footprint", ""),
               c.get("lcsc", ""), c.get("mpn", ""))
        groups.setdefault(key, []).append(c["ref"])
    with open(Path(out) / "BOM.csv", "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["Comment", "Designator", "Footprint", "LCSC Part #", "MPN"])
        for (val, fpr, lcsc, mpn), refs in sorted(groups.items()):
            wr.writerow([val, ",".join(sorted(refs)), fpr.split(":")[-1], lcsc, mpn])
    if not Path(pos).is_file():
        return True, False
    with open(pos, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    with open(Path(out) / "CPL.csv", "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        for r0 in rows:
            wr.writerow([r0.get("Ref"), r0.get("PosX"), r0.get("PosY"),
                         r0.get("Side"), r0.get("Rot")])
    return True, True


def _not_legacy_signature(shipped: object) -> str:
    """Why `shipped` cannot be the legacy writer by its signature ("" = it can)."""
    if not callable(shipped):
        return "is gone"
    try:
        signature = inspect.signature(shipped)
        signature.bind("netlist.json", "out", "board-pos.csv")
    except (TypeError, ValueError) as exc:
        return f"no longer takes (netlist, out, pos): {exc}"
    first = next(iter(signature.parameters), "")
    return "" if first == "netlist" else f"takes {first!r} first, not the netlist path"


def _shipped_legacy(netlist: str, out: str, pos: str) -> tuple[bool, bool]:
    """board_fab.write_assembly_csvs, looked up at call time, while it is still
    the legacy writer: netlist path in, (bom, cpl) out. Once F46-21 rewires it
    (another signature, a writer that rejects a path, or a result that is not
    the (bool, bool) pair) comparing against it proves nothing, so the case
    skips with the reason; the frozen copy keeps guarding the golden."""
    shipped = getattr(board_fab, "write_assembly_csvs", None)
    why = _not_legacy_signature(shipped)
    if why:
        pytest.skip(f"board_fab.write_assembly_csvs {why}; {SHIPPED_SKIP}")
    try:
        result = shipped(netlist, out, pos)
    except (TypeError, ValueError, AttributeError) as exc:
        pytest.skip(f"board_fab.write_assembly_csvs rejects a netlist path ({exc!r}); "
                    f"{SHIPPED_SKIP}")
    if not (isinstance(result, tuple) and len(result) == 2
            and all(isinstance(flag, bool) for flag in result)):
        pytest.skip(f"board_fab.write_assembly_csvs returns {type(result).__name__}, not the "
                    f"legacy (bom, cpl) pair; {SHIPPED_SKIP}")
    return result


LEGACY_WRITERS = [pytest.param(_frozen_legacy, id="frozen-legacy"),
                  pytest.param(_shipped_legacy, id="board_fab")]


def _golden_synthetic() -> dict:
    """No dnp / in_bom / board_only; groups whose natural and lexicographic
    designator orders agree (the legacy writer sorts lexicographically)."""
    return _design(
        _comp("R3", "10k", lcsc="C25804", mpn="0603WAF1002T5E"),
        _comp("R1", "10k", lcsc="C25804", mpn="0603WAF1002T5E"),
        _comp("R2", "4k7", lcsc="C23162"),
        _comp("C2", "4.7µF", "Capacitor_SMD:C_0805_2012Metric", lcsc="C1779"),
        _comp("C1", "4.7µF", "Capacitor_SMD:C_0805_2012Metric", lcsc="C1779"),
        _comp("C3", "100nF", "Capacitor_SMD:C_0402_1005Metric", side="B"),
        _comp("U1", "ESP32-C3-WROOM-02", "RF_Module:ESP32-C3-WROOM-02",
              mpn="ESP32-C3-WROOM-02-N4"),
        _comp("J1", "Conn, 2-pin \"JST\"", "Connector_JST:JST_PH_B2B-PH-K_1x02_P2.00mm_Vertical"),
        _comp("L1", "2.2µH ±20%", "Inductor_SMD:L_1210_3225Metric", dnp=False, in_bom=True),
    )


# ----------------------------------------------------------------------------
# bom_rows
# ----------------------------------------------------------------------------
def test_bom_excludes_dnp_in_bom_false_and_board_only_with_reasons() -> None:
    design = _design(_comp("R1"), _comp("R2", dnp=True), _comp("R3", in_bom=False),
                     _comp("H1", "MountingHole", "MountingHole:MountingHole_3.2mm_M3",
                           board_only=True),
                     _comp("R4", dnp=False, in_bom=True, board_only=False))
    rows, excluded = af.bom_rows(design)
    assert rows == [("10k", "R1,R4", "R_0603_1608Metric", "", "")]
    assert excluded == {"H1": af.REASON_BOARD_ONLY, "R2": af.REASON_DNP,
                        "R3": af.REASON_NOT_IN_BOM}
    assert list(excluded) == ["H1", "R2", "R3"]


def test_every_reason_of_a_part_is_reported() -> None:
    design = _design(_comp("FID1", dnp=True, in_bom=False, board_only=True), _comp("R1"))
    _, excluded = af.bom_rows(design)
    assert excluded == {"FID1": "dnp, in_bom false, board_only"}


def test_flags_are_json_booleans_only() -> None:
    # netlist_schema types the D4 flags as booleans; anything else is not a flag
    design = _design(_comp("R1", dnp="true"), _comp("R2", in_bom=0), _comp("R3", board_only=1))
    rows, excluded = af.bom_rows(design)
    assert excluded == {}
    assert rows[0][1] == "R1,R2,R3"


def test_grouping_by_value_footprint_lcsc_mpn() -> None:
    design = _design(
        _comp("R1", "10k", lcsc="C25804"), _comp("R2", "10k", lcsc="C25804"),
        _comp("R3", "10k", lcsc="C99999"),                       # other LCSC part
        _comp("R4", "10k", "Resistor_SMD:R_0402_1005Metric", lcsc="C25804"),
        _comp("R5", "10k", lcsc="C25804", mpn="RC0603FR-0710KL"),
        _comp("R6", "1k", lcsc="C25804"))
    rows, _ = af.bom_rows(design)
    assert rows == [
        ("10k", "R4", "R_0402_1005Metric", "C25804", ""),
        ("10k", "R1,R2", "R_0603_1608Metric", "C25804", ""),
        ("10k", "R5", "R_0603_1608Metric", "C25804", "RC0603FR-0710KL"),
        ("10k", "R3", "R_0603_1608Metric", "C99999", ""),
        ("1k", "R6", "R_0603_1608Metric", "C25804", ""),
    ]


def test_missing_and_null_fields_group_together_as_empty() -> None:
    design = _design(_comp("R1", lcsc=None, mpn=None), _comp("R2"),
                     {"ref": "TP1", "lib_id": "Connector:TestPoint", "footprint": "TP:TP_1mm",
                      "value": None})
    rows, _ = af.bom_rows(design)
    assert rows == [("", "TP1", "TP_1mm", "", ""), ("10k", "R1,R2", "R_0603_1608Metric", "", "")]


def test_non_string_values_are_written_as_text() -> None:
    rows, _ = af.bom_rows(_design(_comp("R1", 100), _comp("R2", 100)))
    assert rows == [("100", "R1,R2", "R_0603_1608Metric", "", "")]


def test_designators_are_natural_sorted() -> None:
    refs = ["R10", "R2", "R1", "R100", "R20", "R3"]
    design = _design(*(_comp(r) for r in refs),
                     *(_comp(r, dnp=True) for r in ("D10", "D9", "D1")))
    rows, excluded = af.bom_rows(design)
    assert rows[0][1] == "R1,R2,R3,R10,R20,R100"
    assert list(excluded) == ["D1", "D9", "D10"]


def test_natural_sort_handles_multi_part_and_odd_refs() -> None:
    assert af.natural_sorted(["U1B", "U10A", "U1A", "U2", "R01", "R1", "1", "SW_10", "SW_9"]) == [
        "1", "R01", "R1", "SW_9", "SW_10", "U1A", "U1B", "U2", "U10A"]


def test_module_is_stdlib_only() -> None:
    # KiCad-python children may import it (RULES: stdlib only, Python 3.11)
    tree = ast.parse((TOOLS / "assembly_files.py").read_text(encoding="utf-8"))
    roots = {alias.name.split(".")[0] for node in ast.walk(tree)
             if isinstance(node, ast.Import) for alias in node.names}
    roots |= {node.module.split(".")[0] for node in ast.walk(tree)
              if isinstance(node, ast.ImportFrom) and node.module}
    assert roots - set(sys.stdlib_module_names) == set()


def test_importing_the_module_leaves_sys_path_alone(tmp_path: Path) -> None:
    # board_fab (F46-21) imports it lazily; the import must not add server/tools
    code = ("import importlib.util, sys\n"
            "before = list(sys.path)\n"
            "spec = importlib.util.spec_from_file_location('assembly_files', sys.argv[1])\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "print(sys.path == before, ','.join(mod.natural_sorted(['R10', 'R2', 'R1'])))\n")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run([sys.executable, "-c", code, str(TOOLS / "assembly_files.py")],
                          cwd=tmp_path, env=env, capture_output=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert proc.stdout.split() == ["True", "R1,R2,R10"]


def test_design_is_not_mutated(tmp_path: Path) -> None:
    design = _golden_synthetic()
    design["components"].append(_comp("R9", dnp=True))
    before = copy.deepcopy(design)
    pos = _write_pos(tmp_path / "b-pos.csv", _pos_for(design))
    af.bom_rows(design)
    af.cpl_rows(design, af.read_pos_csv(pos))
    af.write_assembly_csvs(design, tmp_path / "out", pos)
    assert design == before


@pytest.mark.parametrize("design, message", [
    ([], "design must be a JSON object"),
    ({"components": {"R1": {}}}, "'components' must be a list"),
    ({"components": ["R1"]}, "component #0 is not an object"),
    ({"components": [{"value": "10k"}]}, "component #0 has no 'ref'"),
    ({"components": [{"ref": ""}]}, "component #0 has no 'ref'"),
    ({"components": [{"ref": 7}]}, "component #0 has no 'ref'"),
])
def test_malformed_designs_fail_loudly(design: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        af.bom_rows(design)  # type: ignore[arg-type]


def test_design_without_components_gives_an_empty_bom() -> None:
    assert af.bom_rows({"name": "empty"}) == ([], {})
    assert af.bom_rows({"components": None}) == ([], {})


# ----------------------------------------------------------------------------
# read_pos_csv
# ----------------------------------------------------------------------------
def test_read_pos_csv_parses_kicad_cli_output(tmp_path: Path) -> None:
    pos = _write_pos(tmp_path / "b-pos.csv", [
        _pos_line("C1", "4.7µF", "C_0805_2012Metric", 83.185, -83.82, 0, "top"),
        _pos_line("U1", "MCU, \"QFN\"", "QFN-32", 1.5, 2.25, -90, "bottom")])
    rows = af.read_pos_csv(pos)
    assert rows == [
        {"Ref": "C1", "Val": "4.7µF", "Package": "C_0805_2012Metric", "PosX": "83.185000",
         "PosY": "-83.820000", "Rot": "0.000000", "Side": "top"},
        {"Ref": "U1", "Val": "MCU, \"QFN\"", "Package": "QFN-32", "PosX": "1.500000",
         "PosY": "2.250000", "Rot": "-90.000000", "Side": "bottom"}]


def test_read_pos_csv_accepts_utf8_bom_and_short_rows(tmp_path: Path) -> None:
    pos = _write_pos(tmp_path / "b-pos.csv", ['"R1","10k","R_0603",1.0,2.0'], bom=True)
    assert af.read_pos_csv(str(pos)) == [
        {"Ref": "R1", "Val": "10k", "Package": "R_0603", "PosX": "1.0", "PosY": "2.0",
         "Rot": "", "Side": ""}]


def test_read_pos_csv_of_a_board_without_parts(tmp_path: Path) -> None:
    assert af.read_pos_csv(_write_pos(tmp_path / "b-pos.csv", [])) == []


@pytest.mark.parametrize("content, message", [
    (b"", "no header"),
    (b"Designator,Mid X,Mid Y,Layer,Rotation\r\nR1,1,2,top,0\r\n", "missing column"),
    (b"Ref,Val,Package,PosX,PosY,Rot,Side\r\n\"" + b"x" * 200_000 + b"\",1\r\n",
     "not a readable"),                                   # csv.Error: field limit
    (b"Ref,Val,Package,PosX,PosY,Rot,Side\r\n\"R\xff1\",x\r\n", "not a readable"),
], ids=["empty", "cpl-header", "huge-field", "not-utf8"])
def test_read_pos_csv_rejects_non_pos_files(tmp_path: Path, content: bytes,
                                            message: str) -> None:
    path = tmp_path / "bad-pos.csv"
    path.write_bytes(content)
    with pytest.raises(ValueError, match=message):
        af.read_pos_csv(path)


# ----------------------------------------------------------------------------
# cpl_rows
# ----------------------------------------------------------------------------
def test_cpl_rows_only_place_bom_parts_in_pos_order() -> None:
    design = _design(_comp("R1"), _comp("R2", dnp=True), _comp("R3"),
                     _comp("H1", "MountingHole", "MountingHole:MountingHole_3.2mm_M3",
                           board_only=True), _comp("R4", in_bom=False))
    pos = [{"Ref": r, "PosX": f"{i}.0", "PosY": "-1.5", "Rot": "90.000000", "Side": "top"}
           for i, r in enumerate(["H1", "R3", "R1", "R2", "R4"])]
    rows, warnings = af.cpl_rows(design, pos)
    assert rows == [("R3", "1.0", "-1.5", "top", "90.000000"),
                    ("R1", "2.0", "-1.5", "top", "90.000000")]
    assert warnings == []


def test_cpl_rows_warn_about_pos_and_design_disagreements() -> None:
    design = _design(_comp("R1"), _comp("R10"), _comp("R2"), _comp("C1", dnp=True))
    pos = [{"Ref": "R1", "PosX": "1", "PosY": "2", "Rot": "0", "Side": "top"},
           {"Ref": "LOGO10", "PosX": "3", "PosY": "4", "Rot": "0", "Side": "top"},
           {"Ref": "LOGO9", "PosX": "3", "PosY": "4", "Rot": "0", "Side": "top"},
           {"Ref": "R1", "PosX": "9", "PosY": "9", "Rot": "0", "Side": "top"},
           {"Ref": "", "PosX": "5", "PosY": "6", "Rot": "0", "Side": "top"},
           {"PosX": "5", "PosY": "6"}]
    rows, warnings = af.cpl_rows(design, pos)
    assert rows == [("R1", "1", "2", "top", "0")]
    assert warnings == [
        "pos row 5 has no Ref; skipped",
        "pos row 6 has no Ref; skipped",
        "pos ref R1 appears 2 times; the first placement is used",
        "pos ref LOGO9 is not in the design; no CPL row",
        "pos ref LOGO10 is not in the design; no CPL row",
        "BOM part R2 has no placement in the pos file; no CPL row",
        "BOM part R10 has no placement in the pos file; no CPL row",
    ]


def test_duplicate_pos_rows_of_an_excluded_part_are_dropped_silently() -> None:
    # no placement of R2 is used, so "the first placement is used" would mislead
    design = _design(_comp("R1"), _comp("R2", dnp=True))
    pos = [{"Ref": r, "PosX": "1", "PosY": "2", "Rot": "0", "Side": "top"}
           for r in ("R1", "R2", "R2")]
    rows, warnings = af.cpl_rows(design, pos)
    assert rows == [("R1", "1", "2", "top", "0")]
    assert warnings == []


def test_cpl_rows_write_missing_cells_as_empty() -> None:
    rows, _ = af.cpl_rows(_design(_comp("R1")), [{"Ref": "R1", "PosX": None}])
    assert rows == [("R1", "", "", "", "")]


# ----------------------------------------------------------------------------
# write_assembly_csvs
# ----------------------------------------------------------------------------
def test_write_assembly_csvs_excludes_flagged_parts_from_both_files(tmp_path: Path) -> None:
    design = _design(_comp("R1", lcsc="C25804"), _comp("R2", lcsc="C25804", dnp=True),
                     _comp("R10", lcsc="C25804"), _comp("C1", "100nF", in_bom=False),
                     _comp("FID1", "Fiducial", "Fiducial:Fiducial_1mm_Mask2mm",
                           board_only=True))
    pos = _write_pos(tmp_path / "b-pos.csv", [
        *_pos_for(design), _pos_line("G1", "LOGO", "Logo", 1, 1, 0, "top")])
    out = tmp_path / "fab"
    res = af.write_assembly_csvs(design, out, pos)
    assert res == {
        "bom": True, "cpl": True, "bom_lines": 1, "cpl_rows": 2,
        "excluded": {"C1": "in_bom false", "FID1": "board_only", "R2": "dnp"},
        "warnings": ["pos ref G1 is not in the design; no CPL row"]}
    assert _read_csv(out / "BOM.csv") == [
        list(af.BOM_HEADER), ["10k", "R1,R10", "R_0603_1608Metric", "C25804", ""]]
    cpl = _read_csv(out / "CPL.csv")
    assert cpl[0] == list(af.CPL_HEADER)
    assert [r[0] for r in cpl[1:]] == ["R1", "R10"]
    json.dumps(res)                                     # export_fab_package returns it


@pytest.mark.parametrize("pos", ["", None, "does-not-exist-pos.csv"])
def test_no_pos_file_writes_the_bom_only(tmp_path: Path, pos: str | None) -> None:
    out = tmp_path / "fab"
    pos_arg = str(tmp_path / pos) if pos else pos
    res = af.write_assembly_csvs(_design(_comp("R1"), _comp("R2", dnp=True)), out, pos_arg)
    assert (res["bom"], res["cpl"], res["bom_lines"], res["cpl_rows"]) == (True, False, 1, 0)
    assert res["excluded"] == {"R2": "dnp"}
    assert res["warnings"] == [f"no pick-and-place file {pos_arg or ''!r}; CPL.csv not written"]
    assert (out / "BOM.csv").is_file() and not (out / "CPL.csv").exists()


def test_unreadable_pos_file_writes_the_bom_only(tmp_path: Path) -> None:
    bad = tmp_path / "b-pos.csv"
    bad.write_text("Designator,X\r\nR1,1\r\n", encoding="utf-8")
    res = af.write_assembly_csvs(_design(_comp("R1")), tmp_path, bad)
    assert (res["bom"], res["cpl"]) == (True, False)
    assert res["warnings"][0].startswith("pick-and-place file unreadable: ")
    assert "missing column" in res["warnings"][0]
    assert not (tmp_path / "CPL.csv").exists()


def test_a_fully_excluded_design_still_writes_headers(tmp_path: Path) -> None:
    design = _design(_comp("H1", board_only=True))
    pos = _write_pos(tmp_path / "b-pos.csv", [])
    res = af.write_assembly_csvs(design, tmp_path, pos)
    assert (res["bom_lines"], res["cpl_rows"]) == (0, 0)
    bom_header = b"Comment,Designator,Footprint,LCSC Part #,MPN\r\n"
    assert (tmp_path / "BOM.csv").read_bytes() == bom_header
    assert (tmp_path / "CPL.csv").read_bytes() == b"Designator,Mid X,Mid Y,Layer,Rotation\r\n"


def test_non_ascii_values_pass_through_unchanged(tmp_path: Path) -> None:
    design = _design(_comp("C1", "4.7µF", "Capacitor_SMD:C_0805_2012Metric"),
                     _comp("R1", "1kΩ ±1%"), _comp("U1", "温度传感器", "Sensor:Ü-Pkg"))
    pos = _write_pos(tmp_path / "b-pos.csv", _pos_for(design))
    af.write_assembly_csvs(design, tmp_path, pos)
    bom = (tmp_path / "BOM.csv").read_bytes()
    for text in ("4.7µF", "1kΩ ±1%", "温度传感器", "Ü-Pkg"):
        assert text.encode("utf-8") in bom
    assert not bom.startswith(b"\xef\xbb\xbf")


# ----------------------------------------------------------------------------
# golden: an unflagged design's bytes equal the legacy writer's
# ----------------------------------------------------------------------------
def _golden_case(name: str) -> dict:
    return _golden_synthetic() if name == "synthetic" else load_example(name)


@pytest.mark.parametrize("legacy", LEGACY_WRITERS)
@pytest.mark.parametrize("case", ["synthetic", "esp32c3_env_node", "buck_5v3a"])
def test_unflagged_design_matches_legacy_bytes(tmp_path: Path, legacy: LegacyWriter,
                                               case: str) -> None:
    design = _golden_case(case)
    netlist = tmp_path / "netlist.json"
    netlist.write_text(json.dumps(design, ensure_ascii=False), encoding="utf-8")
    pos = _write_pos(tmp_path / "board-pos.csv", _pos_for(design))
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    assert legacy(str(netlist), str(old), str(pos)) == (True, True)
    res = af.write_assembly_csvs(design, new, pos)
    assert (res["bom"], res["cpl"], res["excluded"], res["warnings"]) == (True, True, {}, [])
    for name in ("BOM.csv", "CPL.csv"):
        assert (new / name).read_bytes() == (old / name).read_bytes(), name


@pytest.mark.parametrize("legacy", LEGACY_WRITERS)
def test_unflagged_design_without_pos_matches_legacy_bom(tmp_path: Path,
                                                         legacy: LegacyWriter) -> None:
    design = _golden_synthetic()
    netlist = tmp_path / "netlist.json"
    netlist.write_text(json.dumps(design), encoding="utf-8")
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    missing = str(tmp_path / "nope-pos.csv")
    assert legacy(str(netlist), str(old), missing) == (True, False)
    res = af.write_assembly_csvs(design, new, missing)
    assert (res["bom"], res["cpl"]) == (True, False)
    assert (new / "BOM.csv").read_bytes() == (old / "BOM.csv").read_bytes()


def test_golden_fixture_has_the_shapes_it_must_cover() -> None:
    comps = _golden_synthetic()["components"]
    assert not any(c.get("dnp") is True or c.get("in_bom") is False or c.get("board_only")
                   for c in comps)
    rows, _ = af.bom_rows(_golden_synthetic())
    assert any("," in r[1] for r in rows)                       # grouped lines
    assert any(r[3] and r[4] for r in rows)                     # LCSC + MPN
    assert any(not r[0].isascii() for r in rows)                # 'µ' passes through
    assert any(":" in c["footprint"] for c in comps)            # library prefix stripped


# Shapes F46-21 may give board_fab.write_assembly_csvs once it calls this module
# (it must keep the name: tests/test_board_pipeline_split.py checks it exists).
def _rewired_path_to_dict(netlist: str, out: str, pos: str) -> dict:
    return af.write_assembly_csvs(json.loads(Path(netlist).read_text(encoding="utf-8")),
                                  out, pos)


def _rewired_design_named_netlist(netlist: dict, out: str, pos: str) -> dict:
    return af.write_assembly_csvs(netlist, out, pos)


def _rewired_design_first(design: dict, out: str, pos: str) -> dict:
    return af.write_assembly_csvs(design, out, pos)


def _rewired_path_to_triple(netlist: str, out: str, pos: str) -> tuple[bool, bool, dict]:
    res = _rewired_path_to_dict(netlist, out, pos)
    return res["bom"], res["cpl"], res


def _rewired_extra_argument(netlist: str, out: str, pos: str, design: dict) -> dict:
    return af.write_assembly_csvs(design, out, pos)


def _rewired_keyword_only(netlist: str, *, out: str, pos: str) -> dict:
    return _rewired_path_to_dict(netlist, out, pos)


@pytest.mark.parametrize("rewired", [
    _rewired_path_to_dict, _rewired_design_named_netlist, _rewired_design_first,
    _rewired_path_to_triple, _rewired_extra_argument, _rewired_keyword_only, None,
], ids=["path-to-dict", "design-named-netlist", "design-first", "path-to-triple",
        "extra-argument", "keyword-only", "gone"])
def test_a_rewired_shipped_writer_skips_the_shipped_case(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rewired: object) -> None:
    """F46-21 must never be blocked by this file: whatever shape the shipped
    writer takes, the board_fab case skips (with the reason) instead of failing."""
    monkeypatch.setattr(board_fab, "write_assembly_csvs", rewired, raising=False)
    netlist = tmp_path / "netlist.json"
    netlist.write_text(json.dumps(_golden_synthetic()), encoding="utf-8")
    pos = _write_pos(tmp_path / "board-pos.csv", _pos_for(_golden_synthetic()))
    with pytest.raises(pytest.skip.Exception, match=SHIPPED_SKIP):
        _shipped_legacy(str(netlist), str(tmp_path), str(pos))


def test_a_legacy_shaped_shipped_writer_is_compared(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(board_fab, "write_assembly_csvs", _frozen_legacy)
    netlist = tmp_path / "netlist.json"
    netlist.write_text(json.dumps(_golden_synthetic()), encoding="utf-8")
    assert _shipped_legacy(str(netlist), str(tmp_path), str(tmp_path / "no-pos.csv")) == (
        True, False)
    assert (tmp_path / "BOM.csv").is_file()


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def test_main_writes_the_pair_and_prints_a_summary(tmp_path: Path,
                                                   capsys: pytest.CaptureFixture) -> None:
    design = _design(_comp("R1"), _comp("R2", dnp=True))
    netlist = tmp_path / "netlist.json"
    netlist.write_text(json.dumps(design), encoding="utf-8")
    pos = _write_pos(tmp_path / "b-pos.csv", _pos_for(design))
    assert af.main([str(netlist), str(tmp_path / "fab"), str(pos)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert (summary["bom"], summary["cpl"], summary["excluded"]) == (True, True, {"R2": "dnp"})
    assert af.main([str(netlist), str(tmp_path / "fab2")]) == 0
    assert json.loads(capsys.readouterr().out)["cpl"] is False


@pytest.mark.parametrize("content", [None, "{not json", "[]"])
def test_main_reports_bad_input_with_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture,
                                            content: str | None) -> None:
    netlist = tmp_path / "netlist.json"
    if content is not None:
        netlist.write_text(content, encoding="utf-8")
    assert af.main([str(netlist), str(tmp_path / "fab")]) == 2
    assert "assembly_files:" in capsys.readouterr().err


def test_cli_runs_as_a_script(tmp_path: Path) -> None:
    netlist = tmp_path / "netlist.json"
    netlist.write_text(json.dumps(_design(_comp("R1"))), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(TOOLS / "assembly_files.py"), str(netlist),
                           str(tmp_path / "fab")], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                          timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["bom_lines"] == 1
