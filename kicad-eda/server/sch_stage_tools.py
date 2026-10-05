"""Schematic-stage tools of the net-map pipeline (netlist.json -> .kicad_sch),
split out of kicad_server so the server entry point stays small.

  * _build_via_sch_build: the file route of
    kicad_server.build_schematic_from_netlist (server/tools/sch_build.py under
    this venv), used when KiCad 11's schematic API is unavailable;
  * build_pin_sidecars: pin-coordinate sidecars for the file-backend wiring
    tools (server/tools/build_pin_sidecars.py);
  * design_report: design-intent + DFA review of a netlist
    (server/tools/design_report.py).

kicad_server imports these names back and serves the two tools from its own
TOOLS tuple (same names, same order as before the split), so callers keep
using k.build_pin_sidecars / k.design_report. Toolchain functions are looked
up on the module at call time (`tc.run`, `tc.symbols_dir`), so a patch of
toolchain reaches them. This module never imports kicad_server (no cycle):
the API route and the schematic session registry stay there.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import kicad_ipc as ipc
import toolchain as tc

TOOLS_DIR = tc.TOOLS_DIR
PINS_DIR = tc.PINS_DIR


def _build_via_sch_build(netlist: str, out: str, symbols_dir: str, sidecars_dir: str) -> dict:
    """server/tools/sch_build.py under this venv (sexpdata)."""
    script = TOOLS_DIR / "sch_build.py"
    if not script.is_file():
        return {"ok": False, "reason": f"missing {script}"}
    syms = tc.symbols_dir(symbols_dir)
    sides = sidecars_dir or str(PINS_DIR)
    if not syms:
        return {"ok": False, "reason": "symbols_dir not given and KiCad share dir not found"}
    guard = ipc.write_guard(out)
    if guard:
        return guard
    r = tc.run([sys.executable, str(script), netlist, out, syms, sides])
    res = tc.last_json(r.stdout) or {}
    res["ok"] = r.returncode == 0 and res.get("ok", False)
    res["sch"] = out
    if r.stderr:
        res["stderr"] = r.stderr[-1200:]
    return res


def build_pin_sidecars(symbols: str, out_dir: str = "", symbols_dir: str = "") -> dict:
    """Build pin-coordinate sidecars (pin name/number -> schematic-space offset) for
    a comma-separated list of `Lib:Symbol` ids, used by the file-backend wiring
    tools. Writes <Lib_Symbol>.pins.json into out_dir (default: this server's pins/)."""
    script = TOOLS_DIR / "build_pin_sidecars.py"
    if not script.is_file():
        return {"ok": False, "reason": f"missing {script}"}
    syms = tc.symbols_dir(symbols_dir)
    out = out_dir or str(PINS_DIR)
    if not syms:
        return {"ok": False, "reason": "symbols_dir not given and KiCad share dir not found"}
    ids = [s.strip() for s in symbols.split(",") if s.strip()]
    if not ids:
        raise ValueError("symbols must list at least one Library:Symbol id")
    r = tc.run([sys.executable, str(script), syms, out] + ids)
    summary = tc.last_json(r.stdout) or {}
    return {"ok": r.returncode == 0 and summary.get("ok", r.returncode == 0),
            "out_dir": out, "written": summary.get("written", []),
            "failed": summary.get("failed", []),
            "stdout": r.stdout[-800:], "stderr": r.stderr[-800:]}


def design_report(netlist: str, symbols_dir: str = "", out_md: str = "") -> dict:
    """Audit a netlist for design intent + assembly (DFA): part-count reduction
    (repeated passives -> arrays), decoupling coverage (every IC power rail has a
    bypass cap), the power tree (each rail's driver + load count), floating pins,
    and nets that likely need a pull resistor. Reads pin electrical types from the
    KiCad symbols. Returns the structured report and writes a Markdown copy next to
    the netlist. This reviews; it does not modify the design. ok is False (with
    `errors`) when a symbol could not be read: the pin-type checks did not cover
    that part, so the review is incomplete rather than clean."""
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))
    try:
        import design_report as dr
    except Exception as e:
        return {"ok": False, "reason": f"design_report import failed: {e}"}
    design = json.loads(Path(netlist).read_text(encoding="utf-8"))
    syms = tc.symbols_dir(symbols_dir) or ""
    rep = dr.analyze(design, syms)
    md = dr.to_markdown(rep)
    md_path = out_md or (str(Path(netlist).with_suffix("")) + "_report.md")
    Path(md_path).write_text(md, encoding="utf-8")
    errors = list(rep.get("errors", []))
    res = {"ok": not errors, "errors": errors, "report": rep, "markdown": md,
           "markdown_path": md_path}
    if errors:
        res["reason"] = (f"design review could not run for {len(errors)} symbol(s): "
                         + "; ".join(errors))
    return res
