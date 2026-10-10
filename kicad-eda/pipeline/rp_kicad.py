"""KiCad subprocess wrappers of the pipeline harness: every kicad-cli and
KiCad-python child the stages start (DRC, renders, the SWIG gate / fix /
EMC-stitch scripts). Stages call them through this module object
(rp_kicad.drc(...)) so a test patches one place; a new KiCad wrapper goes
here.

Every child runs through toolchain.run: stdin closed (an inherited MCP stdio
pipe would block it), output decoded as UTF-8 with replacement, and a
timeout. DRC goes through drc_guard.run_drc (D7-11): the report is always
THIS run's, and any failure raises."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import rp_paths  # first: puts server/ and server/tools/ on sys.path
import drc_guard
import kicad_server as k
import toolchain as tc

SERVER = rp_paths.SERVER
GATE_TIMEOUT_S = 300          # swig_gate_checks / swig_board_fixes
RENDER_TIMEOUT_S = 300
EMC_TIMEOUT_S = 600
ERROR_LINE = re.compile(r"(^|\s)Error:", re.M)   # kicad-cli (wx may prefix a timestamp)


def board_cli(pcb: str) -> str:
    """The kicad-cli for BOARD jobs on `pcb`: the one of the KiCad that wrote
    the file (toolchain.board_cli: the pcbnew install for a SWIG-built board,
    never a newer nightly's). Loud when absent."""
    cli = tc.board_cli(pcb)
    if not cli:
        raise RuntimeError("kicad-cli not found; install KiCad or set KICAD_PATH")
    return cli


def drc(pcb: str) -> dict:
    """kicad-cli DRC (error severity) of `pcb` with its board kicad-cli,
    through drc_guard.run_drc: <stem>_drc.json is deleted before kicad-cli
    runs, so the counts (and the report the board fixer reads) are always this
    run's. {errors, unconnected, types: {type: count}, kicad_cli}. A failure
    (no board, a non-zero exit, a crash, an `Error:` line, no or an
    unreadable report) raises RuntimeError naming kicad-cli's output tail;
    an older report is never read."""
    cli = board_cli(pcb)
    res = drc_guard.run_drc(pcb, cli, severity="error")
    if "error" in res:
        how = "crashed" if res.get("crash") else "failed"
        raise RuntimeError(f"DRC of {Path(pcb).name} {how}: {res['error']}")
    return {"errors": res["errors"], "unconnected": res["unconnected"],
            "types": res["types"], "kicad_cli": cli}


def gate_check(mode: str, pcb: str, netlist: str) -> dict:
    """Run the pcbnew-side gate checks (connector orientation / keepout /
    plane integrity / routed-width-vs-class) under KiCad's bundled Python.
    Returns {"issues": [...], "warnings": [...]}."""
    kpy = k._kicad_python()
    script = SERVER / "tools" / "swig_gate_checks.py"
    if not kpy or not script.is_file():
        return {"issues": [f"gate check unavailable ({mode})"], "warnings": []}
    r = tc.run([kpy, str(script), mode, pcb, netlist], timeout=GATE_TIMEOUT_S)
    d = tc.last_json(r.stdout or "")
    if d is not None:
        return {"issues": d.get("issues", []), "warnings": d.get("warnings", [])}
    return {"issues": [f"gate check produced no result ({mode}): "
                       f"{(r.stderr or '')[-300:]}"], "warnings": []}


def board_fix(pcb: str, netlist: str, mode: str) -> list:
    """Apply post-route board-level corrective actions (micro-gap heal,
    IPC-2221 parallel vias, EP via farms) under KiCad's Python, from the DRC
    report rp_kicad.drc wrote. Returns the applied log plus every unfixed
    finding as an 'OPEN: ...' entry."""
    kpy = k._kicad_python()
    script = SERVER / "tools" / "swig_board_fixes.py"
    if not kpy or not script.is_file():
        return []
    r = tc.run([kpy, str(script), pcb, netlist, str(drc_guard.report_path(pcb)), mode],
               timeout=GATE_TIMEOUT_S)
    d = tc.last_json(r.stdout or "")
    if d is None:
        return []
    return list(d.get("applied", [])) + [f"OPEN: {o}" for o in d.get("open", [])]


def render(pcb: str, tag: str, quality: str = "basic") -> list:
    """Render top/bottom PNGs next to the board so every gate exit leaves a
    visual record (placement view at G3, final board at G5). Only the PNGs
    THIS run wrote are listed; a failed render is a missing picture, never a
    failed run."""
    base = str(Path(pcb).with_suffix(""))
    cli = board_cli(pcb)
    out = []
    for side in ("top", "bottom"):
        png = f"{base}_{tag}_{side}.png"
        if render_side(cli, pcb, png, side, quality):
            out.append(png)
    return out


def render_side(cli: str, pcb: str, png: str, side: str, quality: str) -> bool:
    """One kicad-cli pcb render; True when this run wrote `png` (an older PNG
    is removed first) and kicad-cli neither failed nor printed an Error line."""
    try:
        Path(png).unlink(missing_ok=True)
        r = tc.run([cli, "pcb", "render", pcb, "-o", png, "--side", side,
                    "--quality", quality, "--width", "1600", "--height", "1400"],
                   timeout=RENDER_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return False
    output = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0 and not ERROR_LINE.search(output) and Path(png).is_file()


def emc_stitch(pcb: str, emc: dict, extra: list[str] | None = None) -> dict:
    """Run the EMC ground-stitching pass (perimeter via fence + interior
    grid) on the routed board under KiCad's Python; `extra` is appended to
    its argv (the features' emc_args, rp_stage_layout.emc_stitch)."""
    kpy = k._kicad_python()
    script = SERVER / "tools" / "emc_stitch.py"
    if not kpy or not script.is_file():
        return {"ok": False, "reason": "emc_stitch unavailable"}
    r = tc.run([kpy, str(script), pcb, str(emc.get("max_freq_hz", 0)),
                "fence" if emc.get("fence_only") else "grid", *(extra or [])],
               timeout=EMC_TIMEOUT_S)
    d = tc.last_json(r.stdout or "")
    return d if d is not None else {"ok": False, "reason": (r.stderr or "")[-300:]}


def courtyard_drc(pcb: str) -> dict:
    """Placement-stage DRC: count only courtyard violations (routing-dependent
    types are G4's job and would false-flag an unrouted board)."""
    d = drc(pcb)
    n = sum(v for t, v in d["types"].items() if "courtyard" in t)
    return {"courtyard_violations": n, "types": d["types"]}
