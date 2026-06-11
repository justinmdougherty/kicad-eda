#!/usr/bin/env python3
"""Controlled-impedance solver (IPC-2141A / Hammerstad-Jensen).

Turns an impedance target + a board stackup into the trace geometry the copper
must use: single-ended microstrip width for a target Z0 (50 ohm RF, etc.) and
the width+gap of an edge-coupled differential pair for a target Zdiff (90 ohm
USB, 100 ohm Ethernet/LVDS). Pure functions, no KiCad.

Honest scope: this sizes GEOMETRY. It cannot field-solve a real stackup or tune
coupled-pair length/skew — that needs a 2D field solver and a router that routes
pairs as pairs (Freerouting does not). The widths/gaps it emits are the correct
manufacturing intent: they let the fab build the board to impedance, DRC enforce
the class, and an interactive/length-tuning pass refine. Reconcile the computed
width with the fab's own impedance table before release.

Microstrip Z0 (Hammerstad, no thickness term — adequate at FR4 tolerances):
  Eeff = (Er+1)/2 + (Er-1)/2 * (1 + 12 H/W)^-0.5
  W/H <= 1:  Z0 = 60/sqrt(Eeff) * ln(8 H/W + W/(4 H))
  W/H >= 1:  Z0 = 120*pi/sqrt(Eeff) / ( W/H + 1.393 + 0.667 ln(W/H + 1.444) )
Edge-coupled differential (standard weak-coupling approximation):
  Zdiff = 2 Z0(W) * (1 - 0.48 exp(-0.96 S/H))
"""
from __future__ import annotations

import math

# A physically-reasonable JLCPCB-class 4-layer 1.6 mm FR4 stackup. `h` is the
# dielectric height from the OUTER signal layer to the nearest reference plane
# (L1->L2), which sets outer-layer microstrip impedance. Reconcile `er`/`h` with
# the fab's published impedance stackup before production.
DEFAULT_STACKUP = {"h": 0.20, "er": 4.20, "t": 0.035}  # mm, -, mm


def microstrip_z0(w: float, h: float, er: float) -> float:
    """Characteristic impedance (ohm) of a microstrip of width `w` over dielectric
    height `h` (both mm) with relative permittivity `er`."""
    if w <= 0 or h <= 0:
        return float("inf")
    u = w / h
    eeff = (er + 1) / 2 + (er - 1) / 2 * (1 + 12 / u) ** -0.5
    if u <= 1:
        return 60 / math.sqrt(eeff) * math.log(8 / u + u / 4)
    return 120 * math.pi / math.sqrt(eeff) / (u + 1.393 + 0.667 * math.log(u + 1.444))


def microstrip_width(z0: float, h: float, er: float,
                     w_lo: float = 0.05, w_hi: float = 5.0) -> float:
    """Solve for the microstrip width (mm) giving impedance `z0`. Bisection —
    Z0 decreases monotonically with width, so the search is well-behaved."""
    for _ in range(80):
        w = (w_lo + w_hi) / 2
        z = microstrip_z0(w, h, er)
        if z > z0:          # too narrow -> widen
            w_lo = w
        else:
            w_hi = w
    return round((w_lo + w_hi) / 2, 4)


def coupled_zdiff(w: float, s: float, h: float, er: float) -> float:
    """Differential impedance of an edge-coupled microstrip pair (width w, gap s)
    via the standard coupling approximation Zdiff = 2 Z0(w)(1 - 0.48 e^(-0.96 s/h))."""
    return 2 * microstrip_z0(w, h, er) * (1 - 0.48 * math.exp(-0.96 * s / h))


def diff_pair(zdiff: float, h: float, er: float, gap: float | None = None) -> dict:
    """Size an edge-coupled differential pair for target `zdiff` (ohm).

    Real controlled-impedance pairs are TIGHTLY coupled (gap ~ trace width) by
    design — for noise immunity and routing density — not because impedance
    requires it (two uncoupled Zdiff/2 traces already give Zdiff). So we adopt the
    industry default S = W (overridable) and solve the width that hits target WITH
    that coupling, then report the achieved Zdiff. The coupling model is a first-
    order approximation: treat the geometry as nominal and confirm against the
    fab's impedance table. Returns {width, gap, zdiff_target, zdiff_achieved, z0_se}."""
    # solve width for target Zdiff under the constraint gap = (gap or width).
    w_lo, w_hi = 0.05, 5.0
    for _ in range(80):
        w = (w_lo + w_hi) / 2
        s = gap if gap is not None else w
        zd = coupled_zdiff(w, s, h, er)
        if zd > zdiff:       # impedance too high -> widen trace
            w_lo = w
        else:
            w_hi = w
    w = round((w_lo + w_hi) / 2, 4)
    s = round(gap if gap is not None else w, 4)
    return {"width": w, "gap": s, "zdiff_target": zdiff,
            "zdiff_achieved": round(coupled_zdiff(w, s, h, er), 1),
            "z0_se": round(microstrip_z0(w, h, er), 2)}


def resolve_impedance(design: dict, stackup: dict | None = None) -> dict:
    """Read controlled-impedance intent off a netlist and return per-net geometry.

    A net opts in via design["net_specs"][net] with either "impedance": <Z0> for
    single-ended (sets track_width) or "diff": <Zdiff> for a differential pair
    (sets diff_pair_width + diff_pair_gap). Returns {net: {...}} only for nets
    that asked for controlled impedance."""
    st = {**DEFAULT_STACKUP, **(design.get("stackup") or {}), **(stackup or {})}
    h, er = st["h"], st["er"]
    out = {}
    for net, s in (design.get("net_specs") or {}).items():
        if "impedance" in s:
            z0 = float(s["impedance"])
            out[net] = {"mode": "single", "z0": z0,
                        "track_width": microstrip_width(z0, h, er),
                        "stackup": {"h": h, "er": er}}
        elif "diff" in s:
            dp = diff_pair(float(s["diff"]), h, er, s.get("gap"))
            out[net] = {"mode": "diff", "zdiff": dp["zdiff_target"],
                        "zdiff_achieved": dp["zdiff_achieved"],
                        "diff_pair_width": dp["width"], "diff_pair_gap": dp["gap"],
                        "track_width": dp["width"], "z0_se": dp["z0_se"],
                        "stackup": {"h": h, "er": er}}
    return out


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) > 1:
        design = json.load(open(sys.argv[1], encoding="utf-8"))
        print(json.dumps(resolve_impedance(design), indent=2))
    else:
        st = DEFAULT_STACKUP
        print(f"Stackup: h={st['h']}mm Er={st['er']} (outer microstrip to L2)")
        for z in (50, 75, 90):
            print(f"  {z} ohm single-ended -> W = {microstrip_width(z, st['h'], st['er'])} mm")
        for zd in (90, 100):
            dp = diff_pair(zd, st["h"], st["er"])
            print(f"  {zd} ohm diff -> W={dp['width']} mm  gap={dp['gap']} mm  "
                  f"(achieved {dp['zdiff_achieved']} ohm, Z0_se={dp['z0_se']})")
