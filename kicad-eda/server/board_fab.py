"""Fab package stage of the net-map pipeline: Gerbers + drill + pick-and-place
with the kicad-cli of the KiCad that wrote the board, the JLCPCB assembly pair
(BOM.csv / CPL.csv) from the netlist, zipped for the fab house.

export() is the body of the MCP tool board_pipeline_tools.export_fab_package
(the tool keeps its name, signature and docstring there as a thin wrapper).
Split out of board_pipeline_tools.py by X-09; later owners locate code here by
function name (write_assembly_csvs, export).

The assembly pair is server/tools/assembly_files' (F46-21): parts flagged
``dnp: true``, ``in_bom: false`` or ``board_only: true`` are left out of
BOM.csv and CPL.csv and come back as ``bom_excluded`` {ref: reason}; pos /
design disagreements and an unreadable netlist are ``assembly_warnings``. The
kicad-cli pos export is unchanged (no --exclude-dnp): the filtering is ours,
so it also holds for footprints that lack the DNP attribute. assembly_files is
imported on first use (server/tools put on sys.path then), never at import.

D7-23a (ADR D7.12): with the stackup artifact (<board stem>.stackup.json or
stackup_json) board_physics writes the fab stackup outputs before the zip.

toolchain is imported AS A MODULE and its functions looked up at call time, so
tests that monkeypatch tc.run / tc.board_cli reach this code.
"""

from __future__ import annotations

import glob
import importlib
import json
import sys
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import board_physics
import toolchain as tc

NOT_WRITTEN = "BOM.csv / CPL.csv not written"


def _assembly_files() -> ModuleType:
    """server/tools/assembly_files (stdlib-only), imported on first use."""
    tools = str(tc.TOOLS_DIR)
    if tools not in sys.path:
        sys.path.insert(0, tools)
    return importlib.import_module("assembly_files")


def _not_written(*warnings: str) -> dict[str, Any]:
    """The assembly summary when neither file was written (a fresh dict)."""
    return {"bom": False, "cpl": False, "bom_lines": 0, "cpl_rows": 0,
            "excluded": {}, "warnings": list(warnings)}


def write_assembly_csvs(netlist: str, out: str, pos: str) -> dict[str, Any]:
    """JLCPCB BOM.csv + CPL.csv in `out` from the netlist.json at `netlist`
    and the kicad-cli pos file `pos`, through assembly_files (the netlist is
    loaded once, here). Returns assembly_files' summary {bom, cpl, bom_lines,
    cpl_rows, excluded {ref: reason}, warnings}. A netlist that cannot be read
    or is not a design writes nothing and is the one warning; a failure to
    write the files raises (OSError), as the legacy writer did."""
    try:
        design = json.loads(Path(netlist).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:     # JSONDecodeError / UnicodeDecodeError too
        return _not_written(f"netlist {netlist} unreadable: {exc}; {NOT_WRITTEN}")
    try:
        return _assembly_files().write_assembly_csvs(design, out, pos)
    except ValueError as exc:                # validated before anything is written
        return _not_written(f"netlist {netlist} malformed: {exc}; {NOT_WRITTEN}")


def _assembly(netlist: str, out: str, pos: str) -> dict[str, Any]:
    """The assembly pair when a netlist is given ('' = none asked for)."""
    if not netlist:
        return _not_written()
    if not Path(netlist).is_file():
        return _not_written(f"netlist {netlist} not found; {NOT_WRITTEN}")
    return write_assembly_csvs(netlist, out, pos)


def export(pcb: str, out_dir: str = "", zip_name: str = "",
           netlist: str = "", stackup_json: str = "") -> dict:
    """export_fab_package's body: Gerbers, drill, pos, the optional BOM/CPL
    pair (flagged parts left out: bom_excluded, assembly_warnings), the fab
    stackup outputs, then the zip (see the tool's docstring in
    board_pipeline_tools)."""
    cli = tc.board_cli(pcb)
    if not cli:
        return {"ok": False, "reason": "kicad-cli not found; install KiCad."}
    found = board_physics.find(pcb, stackup_json)          # D7-23a: the stackup artifact
    if isinstance(found, dict):
        return found
    out = out_dir or (str(Path(pcb).with_suffix("")) + "_gerbers")
    Path(out).mkdir(parents=True, exist_ok=True)
    g = tc.run([cli, "pcb", "export", "gerbers", "-o", out, pcb])
    dr = tc.run([cli, "pcb", "export", "drill", "-o", out, pcb])
    pos = str(Path(out) / (Path(pcb).stem + "-pos.csv"))
    p = tc.run([cli, "pcb", "export", "pos", "-o", pos, "--format", "csv", "--units", "mm", pcb])
    assembly = _assembly(netlist, out, pos)
    stack = board_physics.fab_stackup_outputs(pcb, out, found, cli, netlist=netlist)
    zpath = zip_name or (str(Path(pcb).with_suffix("")) + f"_{tc.FAB_PROFILE}.zip")
    files = [f for f in glob.glob(str(Path(out) / "*")) if not f.endswith(".zip")]
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, arcname=Path(f).name)
    res = {"ok": g.returncode == 0 and dr.returncode == 0,
           "out_dir": out, "zip": zpath, "fab_profile": tc.FAB_PROFILE, "kicad_cli": cli,
           "bom": assembly["bom"], "cpl": assembly["cpl"],
           "bom_excluded": assembly["excluded"], "assembly_warnings": assembly["warnings"],
           "files": [Path(f).name for f in files],
           "stderr": (g.stderr + dr.stderr + p.stderr)[-1200:]}
    return board_physics.with_fab_stackup(res, stack)                  # D7-23a
