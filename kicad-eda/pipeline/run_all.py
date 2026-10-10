#!/usr/bin/env python3
"""Regression suite: run every test-board netlist through the full pipeline and
print a PASS/FAIL matrix. Proves the generator stays green across QFN, LQFP,
multi-unit, and multi-rail boards after any change to the tools.

Usage: <venv-python> run_all.py [boards_dir]
       boards_dir defaults to the plugin's examples/ directory.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import run_pipeline as rp

HERE = (Path(sys.argv[1]).resolve() if len(sys.argv) > 1
        else Path(__file__).resolve().parents[1] / "examples")

# (folder, board_w, board_h) — dims are hints; auto-placement sizes the board.
BOARDS = [
    ("cp2102n_uart", 50, 50),
    ("stm32f103_dev", 50, 50),
    ("lm358_buffer", 30, 30),
    ("usb_afe", 40, 40),
    ("lm324_quad", 35, 35),
    ("buck_5v3a", 30, 60),
    ("rf_coupon_50r", 40, 25),
    ("rf_lna_gainblock", 35, 30),
    ("esp32c3_env_node", 50, 50),     # practical board: module+overhang, holes, BOM/CPL
    ("negative_gate_test", 60, 40),   # self-healing fixture, reset each run
]


def _reset_negative_fixture() -> None:
    """Regenerate the negative gate-test board (buck with its input decoupling
    stripped). The G2 gate must detect the missing bypass, auto-remediate by
    adding it back, and the board must then pass — proving the self-feeding
    review loop on every regression run. Remediation rewrites the netlist, so
    the fixture is re-broken here each time."""
    src = HERE / "buck_5v3a" / "netlist.json"
    dst = HERE / "negative_gate_test"
    if not src.is_file():
        return
    dst.mkdir(exist_ok=True)
    d = json.loads(src.read_text(encoding="utf-8"))
    d["title"] = "NEGATIVE TEST - buck with no input decoupling"
    d["name"] = "negative_gate_test"
    drop = {"C1", "C2"}
    d["components"] = [c for c in d["components"] if c["ref"] not in drop]
    d["nets"] = {n: [p for p in conns if p[0] not in drop]
                 for n, conns in d["nets"].items()}
    (dst / "netlist.json").write_text(json.dumps(d, indent=2), encoding="utf-8")


def _build_g3_fixture() -> str:
    """negative_g3: derived from cp2102n — a USB-C FIXED mid-board with its
    mouth facing the interior (G3 blocker, non-auto) plus a 2x20 THT socket
    wall across the middle that pinches the +3V3 plane between a fixed cap
    below and the auto field above (G3 plane warning). Because the mouth
    blocker stops the run AT G3, this fixture never routes — it proves
    detection, cheaply, every run."""
    src = HERE / "cp2102n_uart" / "netlist.json"
    dst = HERE / "negative_g3"
    dst.mkdir(exist_ok=True)
    d = json.loads(src.read_text(encoding="utf-8"))
    d["title"] = "NEGATIVE TEST - inward connector + plane pinch wall"
    d["name"] = "negative_g3"
    d["board"] = {"w": 54.0, "h": 52.0,
                  "auto_x": 2.0, "auto_y": 2.0, "auto_w": 34.0, "auto_h": 28.0}
    for c in d["components"]:
        if c["ref"] == "J1":               # USB-C: mid-board, mouth inward
            c.update({"x": 45.0, "y": 14.0, "rot": 270})
        if c["ref"] == "C2":               # +3V3 cap stranded below the wall
            c.update({"x": 10.0, "y": 46.0})
    # the wall: pad antipads span ~51 mm, leaving ~1.2 mm fill corridors at
    # each board edge — under the 1.8 mm pinch radius, so G3 must warn
    d["components"].append({
        "ref": "J9", "lib_id": "Connector_Generic:Conn_02x20_Odd_Even",
        "value": "WALL",
        "footprint": "Connector_PinSocket_2.54mm:PinSocket_2x20_P2.54mm_Vertical",
        "x": 27.0, "y": 34.0, "rot": 90, "interior": True})
    d.setdefault("no_connect", [])
    d["no_connect"] += [["J9", str(i)] for i in range(1, 41)]
    (dst / "netlist.json").write_text(json.dumps(d, indent=2),
                                      encoding="utf-8")
    return str(dst / "netlist.json")


def _fixture_g3() -> tuple[str, dict]:
    """Run the G3 fixture and assert it fails AT G3 with both findings. It
    runs strict (napkin=False): napkin mode would turn the inward connector
    out, and the fixture exists to prove DETECTION."""
    netlist = _build_g3_fixture()
    rep = rp.run(netlist, 56, 40, napkin=False)
    g3 = next((g for g in rep.get("gates", []) if g["gate"] == "G3"), {})
    blockers = " | ".join(g3.get("blockers", []))
    warnings = " | ".join(g3.get("warnings", []))
    ok = (not rep.get("PASS")
          and "mating face" in blockers
          and "plane on In2.Cu" in warnings)
    return ("negative_g3", {
        "status": "PASS" if ok else "FAIL",
        "gate": "G3*",                     # expected-fail fixture
        "detail": ("inward-mouth blocker + plane-pinch warning detected"
                   if ok else f"blockers=[{blockers[:120]}] "
                              f"warnings=[{warnings[:120]}]")})


def _fixture_g4_heal() -> tuple[str, dict]:
    """Cut the longest signal track on a COPY of the routed lm358 board,
    then assert the G4 healer (swig_board_fixes) restores DRC to 0
    unconnected — proves the heal path without invoking the router."""
    import shutil
    import subprocess
    src = HERE / "lm358_buffer"
    dst = HERE / "negative_g4_heal"
    dst.mkdir(exist_ok=True)
    board = dst / "netlist.kicad_pcb"
    if not (src / "netlist.kicad_pcb").is_file():
        return ("negative_g4_heal", {"status": "SKIP",
                                     "detail": "no routed lm358 board"})
    shutil.copy(src / "netlist.kicad_pcb", board)
    for ext in (".kicad_pro", ".json"):
        if (src / f"netlist{ext}").is_file():
            shutil.copy(src / f"netlist{ext}", dst / f"netlist{ext}")
    kpy = rp.k._kicad_python()
    cut = subprocess.run(
        [kpy, str(rp.SERVER / "tools" / "swig_cut_track.py"), str(board)],
        capture_output=True, text=True, timeout=120)
    broken = rp._drc(str(board))
    if broken["unconnected"] < 1:
        return ("negative_g4_heal", {
            "status": "FAIL", "detail": f"cut did not break the board "
            f"({cut.stdout.strip()[:80]})"})
    fixes = rp._board_fix(str(board), str(dst / "netlist.json"), "heal")
    healed = rp._drc(str(board))
    ok = healed["unconnected"] == 0 and healed["errors"] == 0 and fixes
    return ("negative_g4_heal", {
        "status": "PASS" if ok else "FAIL",
        "gate": "G4*",
        "detail": (f"cut -> {broken['unconnected']} unconnected -> healed "
                   f"({len(fixes)} fix(es)) -> {healed['unconnected']}"
                   if ok else f"unconn {broken['unconnected']} -> "
                              f"{healed['unconnected']}, fixes={fixes[:2]}")})


def main() -> int:
    _reset_negative_fixture()
    results = []
    for folder, w, h in BOARDS:
        netlist = str(HERE / folder / "netlist.json")
        if not Path(netlist).is_file():
            results.append((folder, "MISSING", {}))
            continue
        rep = rp.run(netlist, w, h)
        sch = rep.get("schematic", {})
        drc = rep.get("drc", {})
        auto = rep.get("autoroute", {})
        failed_gate = next((g["gate"] for g in rep.get("gates", [])
                            if not g["passed"]), "-")
        results.append((folder, "PASS" if rep.get("PASS") else "FAIL", {
            "erc": sch.get("erc_violations"),
            "drc_err": drc.get("errors"),
            "unconn": drc.get("unconnected"),
            "scale": auto.get("pack_scale"),
            "gate": failed_gate,
            "remediated": sum(len(r["applied"])
                              for r in rep.get("remediation", [])),
        }))

    # expected-fail fixtures: each must FAIL at its gate with the right
    # finding (status PASS = the gate caught it)
    for name, m in (_fixture_g3(), _fixture_g4_heal()):
        results.append((name, m.pop("status"), m))

    print("\n=== REGRESSION MATRIX (gated) ===")
    for folder, status, m in results:
        blocked = ("" if m.get("gate") in ("-", None)
                   else f"  AT {m['gate']}")
        fixed = f" fixed={m['remediated']}" if m.get("remediated") else ""
        detail = f"  {m['detail']}" if m.get("detail") else ""
        print(f"  {status:5} {folder:18} erc={m.get('erc')} drc={m.get('drc_err')} "
              f"unconn={m.get('unconn')} scale={m.get('scale')}"
              f"{fixed}{blocked}{detail}")
    allpass = all(s == "PASS" for _, s, _ in results)
    print(f"\nALL PASS: {allpass}")
    print(json.dumps({"all_pass": allpass,
                      "results": [{"board": f, "status": s, **m} for f, s, m in results]}))
    return 0 if allpass else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
