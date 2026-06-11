#!/usr/bin/env python3
"""Thermal review — component dissipation vs package limits (G2 gate input).

Pure netlist math, no pcbnew: runs with the schematic review. The board-side
companion (thermal-via farm under exposed pads) lives in swig_gate_checks.

What it checks, per heat-source class:
  shunts / power resistors   P = I^2 * R vs the DERATED rating (50%, Class 3
                             practice). Rating parsed from the value string
                             ("0.01R 1% 3W") or defaulted from the footprint
                             size. I = series current from net_specs.
  power FETs                 P = I^2 * Rds(on). Rds from component
                             "rds_mohm" (declared -> blocker-grade) else a
                             10 mOhm estimate (warning-grade).
  linear regulators          P = (Vin - Vout) * Iout from net_specs voltages.
  switching regulators       loss ~= 4% of through-power (eta ~0.92 at
                             mid duty) — estimate, warning-grade only.
                             Declare "thermal": {"p_diss": W} to make exact.
  any component              may declare "thermal": {"p_diss", "theta_ja",
                             "tj_max"} — declared numbers gate hard.

Junction check: Tj = Tamb + P * thetaJA  must stay <= margin * Tj_max
(defaults: Tamb 50 C, margin 0.8, Tj_max 150 C).

Returns {"blockers": [...], "warnings": [...], "ep_via_parts": [refs]} —
ep_via_parts are exposed-pad packages dissipating >= EP_VIA_W that the G3
board check must find a thermal via farm under.
"""
from __future__ import annotations

import re

T_AMBIENT = 50.0          # C — Class 3 conservative default
TJ_MAX = 150.0
TJ_MARGIN = 0.8           # use at most 80% of Tj_max
R_DERATE = 0.5            # resistors: 50% power derating
EP_VIA_W = 0.8            # EP parts above this need a via farm (G3 checks)
FET_RDS_EST_MOHM = 10.0
SW_LOSS_FRACTION = 0.04   # switcher loss ~4% of through-power (eta ~0.92)

# theta-JA defaults by package family (K/W, JEDEC 2s2p-ish, EP soldered)
THETA_JA = [
    ("PowerPAK", 50.0), ("HSOP", 40.0), ("DPAK", 60.0), ("D2PAK", 40.0),
    ("TO-252", 60.0), ("TO-263", 40.0), ("QFN", 45.0), ("DFN", 55.0),
    ("SOIC", 110.0), ("SO-8", 110.0), ("TSSOP", 100.0), ("SOT-23", 250.0),
    ("SOT-223", 60.0), ("SOT-89", 100.0),
]
# resistor power rating by footprint size code (W)
R_RATING = {"0402": 0.063, "0603": 0.1, "0805": 0.125, "1206": 0.25,
            "1210": 0.5, "2010": 0.5, "2512": 1.0}
EP_HINTS = ("EP", "ThermalVias", "PowerPAK", "HSOP", "DPAK", "TO-252",
            "TO-263", "D2PAK")


def _parse_ohms(value: str) -> float | None:
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(k|K|m|M|R|r)?(?:\d+)?\b", value or "")
    if not m:
        return None
    v = float(m.group(1))
    suf = (m.group(2) or "R")
    if suf in ("k", "K"):
        return v * 1e3
    if suf == "M":
        return v * 1e6
    if suf == "m":
        return v * 1e-3
    return v


def _parse_rating_w(value: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*W\b", value or "")
    return float(m.group(1)) if m else None


def _theta_ja(footprint: str, declared) -> float:
    if declared:
        return float(declared)
    for hint, th in THETA_JA:
        if hint.lower() in (footprint or "").lower():
            return th
    return 100.0


def _r_rating(footprint: str) -> float:
    for code, w in R_RATING.items():
        if code in (footprint or ""):
            return w
    return 0.1


def _nets_of(design: dict, ref: str) -> list[str]:
    return [n for n, conns in design.get("nets", {}).items()
            if any(c[0] == ref for c in conns)]


def _net_i(design: dict, net: str) -> float:
    return float(design.get("net_specs", {}).get(net, {}).get("current", 0) or 0)


def _net_v(design: dict, net: str) -> float:
    return float(design.get("net_specs", {}).get(net, {}).get("voltage", 0) or 0)


def _series_i(design: dict, ref: str) -> float:
    """Channel/series current estimate: the two highest attached-net currents,
    take the smaller (gate/sense nets carry ~0 and drop out)."""
    cur = sorted((_net_i(design, n) for n in _nets_of(design, ref)),
                 reverse=True)
    return cur[1] if len(cur) >= 2 else 0.0


def check(design: dict) -> dict:
    cfg = design.get("thermal", {}) if isinstance(design.get("thermal"), dict) else {}
    t_amb = float(cfg.get("t_ambient", T_AMBIENT))
    margin = float(cfg.get("tj_margin", TJ_MARGIN))
    blockers, warnings, ep_parts = [], [], []

    for c in design.get("components", []):
        ref = c["ref"]
        fp = c.get("footprint", "")
        val = c.get("value", "")
        lib = c.get("lib_id", "")
        th = c.get("thermal", {}) if isinstance(c.get("thermal"), dict) else {}
        tj_max = float(th.get("tj_max", TJ_MAX))
        tj_lim = t_amb + margin * (tj_max - t_amb)
        declared_p = th.get("p_diss")

        p, hard, kind = None, False, ""
        if declared_p is not None:
            p, hard, kind = float(declared_p), True, "declared p_diss"
        elif lib.startswith("Device:R"):
            ohms = _parse_ohms(val)
            # "thermal": {"i_max": A} declares the part's true worst-case
            # current when topology caps it below the net spec (e.g. a
            # sleep-range shunt that only sees full current when bypassed)
            i = float(th["i_max"]) if "i_max" in th else _series_i(design, ref)
            if ohms is None or i <= 0 or ohms > 100:
                continue                      # signal-level resistor
            p = i * i * ohms
            rated = _parse_rating_w(val)
            limit = (rated if rated is not None else _r_rating(fp)) * R_DERATE
            if p > limit:
                msg = (f"{ref} ({val}): dissipates {p:.2f} W at {i} A but the "
                       f"derated rating is {limit:.2f} W "
                       f"({R_DERATE:.0%} of "
                       f"{rated if rated is not None else _r_rating(fp)} W)")
                (blockers if rated is not None else warnings).append(msg)
            continue                          # resistors: rating check only
        elif "Transistor_FET" in lib:
            i = float(th["i_max"]) if "i_max" in th else _series_i(design, ref)
            if i < 0.5:
                continue                      # small-signal FET
            rds = th.get("rds_mohm", c.get("rds_mohm"))
            hard = rds is not None
            rds_m = float(rds) if rds is not None else FET_RDS_EST_MOHM
            p = i * i * rds_m / 1000.0
            kind = f"I^2*Rds at {i} A, {rds_m} mOhm" + \
                   ("" if hard else " (estimate — declare rds_mohm)")
        elif "Regulator_Linear" in lib:
            volts = sorted((_net_v(design, n) for n in _nets_of(design, ref)),
                           reverse=True)
            vin = volts[0] if volts else 0
            vout = next((v for v in volts if 0 < v < vin), 0)
            iout = max((_net_i(design, n) for n in _nets_of(design, ref)
                        if 0 < _net_v(design, n) < vin), default=0)
            if vin <= 0 or vout <= 0 or iout <= 0:
                continue
            p, hard = (vin - vout) * iout, True
            kind = f"(Vin {vin} - Vout {vout}) * {iout} A"
        elif "Regulator_Switching" in lib:
            nets = _nets_of(design, ref)
            vin = max((_net_v(design, n) for n in nets), default=0)
            i = max((_net_i(design, n) for n in nets), default=0)
            if vin <= 0 or i <= 0:
                continue
            p = SW_LOSS_FRACTION * vin * i
            kind = (f"~{SW_LOSS_FRACTION:.0%} of {vin} V x {i} A through-power "
                    f"(estimate — declare thermal.p_diss for exact)")
        else:
            continue

        theta = _theta_ja(fp, th.get("theta_ja"))
        tj = t_amb + p * theta
        if p >= EP_VIA_W and any(h.lower() in fp.lower() for h in EP_HINTS):
            ep_parts.append(ref)
        if tj > tj_lim:
            msg = (f"{ref}: ~{p:.2f} W ({kind}) x thetaJA {theta} K/W -> "
                   f"Tj ~{tj:.0f} C at Tamb {t_amb} C, over the "
                   f"{margin:.0%} limit ({tj_lim:.0f} C of Tj_max {tj_max} C) "
                   f"— spread copper, add thermal vias, or derate")
            (blockers if hard else warnings).append(msg)

    return {"blockers": blockers, "warnings": warnings,
            "ep_via_parts": sorted(set(ep_parts))}


if __name__ == "__main__":
    import json
    import sys
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    print(json.dumps(check(d), indent=2))
