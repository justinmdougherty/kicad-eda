"""KiCad subprocess wrappers of the pipeline harness: every kicad-cli and
KiCad-python child the stages start (DRC, renders, the SWIG gate / fix /
EMC-stitch scripts). Stages call them through this module object
(rp_kicad.drc(...)) so a test patches one place; a new KiCad wrapper goes
here."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import rp_paths  # first: puts server/ and server/tools/ on sys.path
import kicad_server as k
import toolchain as tc

SERVER = rp_paths.SERVER


def board_cli(pcb: str) -> str:
    """The kicad-cli for BOARD jobs on `pcb`: the one of the KiCad that wrote
    the file (toolchain.board_cli: the pcbnew install for a SWIG-built board,
    never a newer nightly's). Loud when absent."""
    cli = tc.board_cli(pcb)
    if not cli:
        raise RuntimeError("kicad-cli not found; install KiCad or set KICAD_PATH")
    return cli


def drc(pcb: str) -> dict:
    rpt = str(Path(pcb).with_suffix("")) + "_drc.json"
    cli = board_cli(pcb)
    subprocess.run([cli, "pcb", "drc", pcb, "--severity-error",
                    "-o", rpt, "--format", "json"],
                   capture_output=True, text=True, stdin=subprocess.DEVNULL)
    d = json.loads(Path(rpt).read_text())
    return {"errors": len(d.get("violations", [])),
            "unconnected": len(d.get("unconnected_items", [])),
            "types": counts(d.get("violations", [])),
            "kicad_cli": cli}


def counts(viols: list) -> dict:
    out: dict[str, int] = {}
    for v in viols:
        out[v.get("type", "?")] = out.get(v.get("type", "?"), 0) + 1
    return out


def gate_check(mode: str, pcb: str, netlist: str) -> dict:
    """Run the pcbnew-side gate checks (connector orientation / keepout /
    plane integrity / routed-width-vs-class) under KiCad's bundled Python.
    Returns {"issues": [...], "warnings": [...]}."""
    kpy = k._kicad_python()
    script = SERVER / "tools" / "swig_gate_checks.py"
    if not kpy or not script.is_file():
        return {"issues": [f"gate check unavailable ({mode})"], "warnings": []}
    r = subprocess.run([kpy, str(script), mode, pcb, netlist],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300)
    for line in reversed(r.stdout.strip().splitlines()):
        if line.startswith("{"):
            try:
                d = json.loads(line)
                return {"issues": d.get("issues", []),
                        "warnings": d.get("warnings", [])}
            except Exception:
                break
    return {"issues": [f"gate check produced no result ({mode}): "
                       f"{r.stderr[-300:]}"], "warnings": []}


def board_fix(pcb: str, netlist: str, mode: str) -> list:
    """Apply post-route board-level corrective actions (micro-gap heal,
    IPC-2221 parallel vias) under KiCad's Python. Returns the applied log."""
    kpy = k._kicad_python()
    script = SERVER / "tools" / "swig_board_fixes.py"
    drc_rpt = str(Path(pcb).with_suffix("")) + "_drc.json"
    if not kpy or not script.is_file():
        return []
    r = subprocess.run([kpy, str(script), pcb, netlist, drc_rpt, mode],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300)
    for line in reversed(r.stdout.strip().splitlines()):
        if line.startswith("{"):
            try:
                d = json.loads(line)
                return d.get("applied", []) + \
                    [f"OPEN: {o}" for o in d.get("open", [])]
            except Exception:
                break
    return []


def render(pcb: str, tag: str, quality: str = "basic") -> list:
    """Render top/bottom PNGs next to the board so every gate exit leaves a
    visual record (placement view at G3, final board at G5)."""
    out = []
    base = str(Path(pcb).with_suffix(""))
    cli = board_cli(pcb)
    for side in ("top", "bottom"):
        png = f"{base}_{tag}_{side}.png"
        try:
            r = subprocess.run(
                [cli, "pcb", "render", pcb, "-o", png,
                 "--side", side, "--quality", quality,
                 "--width", "1600", "--height", "1400"],
                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300)
            if r.returncode == 0 and Path(png).is_file():
                out.append(png)
        except Exception:
            pass
    return out


def emc_stitch(pcb: str, emc: dict) -> dict:
    """Run the EMC ground-stitching pass (perimeter via fence + interior
    grid) on the routed board under KiCad's Python."""
    kpy = k._kicad_python()
    script = SERVER / "tools" / "emc_stitch.py"
    if not kpy or not script.is_file():
        return {"ok": False, "reason": "emc_stitch unavailable"}
    r = subprocess.run(
        [kpy, str(script), pcb, str(emc.get("max_freq_hz", 0)),
         "fence" if emc.get("fence_only") else "grid"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=600)
    for line in reversed(r.stdout.strip().splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                break
    return {"ok": False, "reason": r.stderr[-300:]}


def courtyard_drc(pcb: str) -> dict:
    """Placement-stage DRC: count only courtyard violations (routing-dependent
    types are G4's job and would false-flag an unrouted board)."""
    d = drc(pcb)
    n = sum(v for t, v in d["types"].items() if "courtyard" in t)
    return {"courtyard_violations": n, "types": d["types"]}
