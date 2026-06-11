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

    print("\n=== REGRESSION MATRIX (gated) ===")
    for folder, status, m in results:
        blocked = "" if m.get("gate") == "-" else f"  BLOCKED AT {m['gate']}"
        fixed = f" fixed={m['remediated']}" if m.get("remediated") else ""
        print(f"  {status:5} {folder:18} erc={m.get('erc')} drc={m.get('drc_err')} "
              f"unconn={m.get('unconn')} scale={m.get('scale')}{fixed}{blocked}")
    allpass = all(s == "PASS" for _, s, _ in results)
    print(f"\nALL PASS: {allpass}")
    print(json.dumps({"all_pass": allpass,
                      "results": [{"board": f, "status": s, **m} for f, s, m in results]}))
    return 0 if allpass else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
