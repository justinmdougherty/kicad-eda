"""part_presets: LG290P, LC29H and the opt-in combined LG290P/LC29H (F2-12, D1).

Unit tier (venv only, no KiCad):
  * PRESETS maps lg290p, lc29h and lg290p_lc29h to (footprint_fn, symbol_spec_fn);
    the combined one is opt-in (get_preset refuses it unless asked);
  * LC29H footprint: 24 oval 2.5 x 0.8 mm pads on 1.1 mm pitch at x = +/-6.1,
    the LG290P edge rows in y, typed here from the LC29H Hardware Design V1.3
    Fig. 19 (not from the module's helpers); courtyard, static checks;
  * LC29H symbol: Table 6 names / types, 2.8 V I/O in the pin notes and the
    description, GND stacked; every preset's pad numbers == its symbol's pins;
  * combined: union edge copper x 4.3..7.35, the LG290P and LC29H lands each
    inside it; per-build paste (LG290P stencil: LG290P lands + inner grid;
    LC29H stencil: LC29H lands, no inner-grid paste); conflicting pins
    1, 4, 5, 6, 7, 15, 16 are N/C on its symbol;
  * the module docstring's decisions.append_decision example runs (doctest);
  * footprints build without sexpdata (KiCad-python safe), symbols need it.
  * netlist hints (F2-S1b): every preset carries lga_strategy's hints on its own
    pad table (lg290p and the combined one: F2-S1's 40 escape vias; lc29h: none,
    no inner grid) and a strategy note naming F2-S1's measured numbers.
Live tier (skipped without KiCad): each preset footprint passes
kicad_parse_problems on the oldest kicad-cli and pcbnew_load; each preset
symbol passes sym_author.validate_library; KiCad 9's Python imports the module
and builds the hints.
"""
from __future__ import annotations

import doctest
import json
import subprocess
import sys
from pathlib import Path

import pytest

import decisions
import fp_author as fa
import fp_author_templates as ft
import kicad_env as env
import lga_strategy as lga
import part_presets as pp
import sym_author as sa

TOOLS = Path(pp.__file__).resolve().parent

# --- LC29H Series Hardware Design V1.3 (2024-07-30), mm, KiCad frame (+Y down) ----
# Fig. 19 "Recommended Footprint", printed page 47 (PDF page 48): the 12.2 mm module-edge
# extension lines run through the pad centres; "1.25" spans the pad's outer end to the
# module edge and "2.5" the pad length, so each pad is centred ON the module edge:
# x = +/-12.2/2 = +/-6.1. ("1.45" / "15.1" belong to the SAFE AREA outline, 0.2 mm
# beyond the pad ends; reading them as the pad extent gives the wrong 6.3.) Measured
# from the PDF's vector drawing: pad centres 12.200 mm apart, pad length 2.500 mm.
LC29H_PAD_X = 6.1
LC29H_EDGE_Y = [-7.0, -5.9, -4.8, -3.7, -2.6, -1.5, -0.4, 2.6, 3.7, 4.8, 5.9, 7.0]  # pins 1-12
# Table 6 (pp. 26-28) / Fig. 4 (p. 25), variants AA / AI / BS / DA
LC29H_PINS = {
    "1": ("WAKEUP", "input"), "2": ("RESERVED", "no_connect"), "3": ("1PPS", "output"),
    "4": ("RESERVED", "no_connect"), "5": ("D_SEL1", "input"), "6": ("D_SEL2", "input"),
    "7": ("VDD_EXT", "power_out"), "8": ("RESET_N", "input"), "9": ("VDD_RF", "power_out"),
    "10": ("GND", "power_in"), "11": ("RF_IN", "input"), "12": ("GND", "power_in"),
    "13": ("GND", "power_in"), "14": ("ANT_ON", "output"), "15": ("TXD2", "output"),
    "16": ("RXD2", "input"), "17": ("RESERVED", "no_connect"),
    "18": ("I2C_SDA/SPI_CS", "bidirectional"), "19": ("I2C_SCL/SPI_CLK", "input"),
    "20": ("TXD1/SPI_MISO", "output"), "21": ("RXD1/SPI_MOSI", "input"),
    "22": ("V_BCKP", "power_in"), "23": ("VCC", "power_in"), "24": ("GND", "power_in"),
}
CONFLICTS = {"1": "RESERVED/WAKEUP", "4": "EVENT/RESERVED", "5": "RESERVED/D_SEL1",
             "6": "TXD2/D_SEL2", "7": "RXD2/VDD_EXT", "15": "RXD3/TXD2", "16": "TXD3/RXD2"}


def lc29h_expected() -> dict[str, tuple[float, float, float, float, str]]:
    exp = {}
    for i, y in enumerate(LC29H_EDGE_Y):
        exp[str(i + 1)] = (-LC29H_PAD_X, y, 2.5, 0.8, "oval")        # left, top to bottom
        exp[str(24 - i)] = (LC29H_PAD_X, y, 2.5, 0.8, "oval")        # right: 24 top .. 13 bottom
    return exp


def by_number(spec: ft.FootprintSpec) -> dict[str, ft.Pad]:
    pads = [p for p in spec.pads if p.number and ft.has_copper(p)]
    out = {p.number: p for p in pads}
    assert len(out) == len(pads), "duplicate copper pad numbers"
    return out


def paste_windows(spec: ft.FootprintSpec) -> list[ft.Pad]:
    return [p for p in spec.pads if "F.Paste" in p.pad_layers]


def covers(outer: tuple, inner: tuple) -> bool:
    return (outer[0] <= inner[0] + 1e-9 and outer[1] <= inner[1] + 1e-9
            and outer[2] >= inner[2] - 1e-9 and outer[3] >= inner[3] - 1e-9)


ALL_FOOTPRINTS = {
    "lg290p": lambda: pp.PRESETS["lg290p"].footprint_fn(),
    "lc29h": lambda: pp.PRESETS["lc29h"].footprint_fn(),
    "lg290p_lc29h": lambda: pp.PRESETS["lg290p_lc29h"].footprint_fn(),
    "lg290p_lc29h-lc29h_stencil": lambda: pp.PRESETS["lg290p_lc29h"].footprint_fn(
        stencil="lc29h"),
}


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
def test_presets_map_names_to_footprint_and_symbol_functions():
    assert set(pp.PRESETS) == {"lg290p", "lc29h", "lg290p_lc29h"}
    for name, preset in pp.PRESETS.items():
        footprint_fn, symbol_spec_fn = preset                  # a plain 2-tuple
        assert isinstance(footprint_fn(), ft.FootprintSpec), name
        assert isinstance(symbol_spec_fn(), sa.SymbolSpec), name
    assert pp.DEFAULT_PRESETS == ("lg290p", "lc29h") and pp.OPT_IN_PRESETS == {"lg290p_lc29h"}
    with pytest.raises(TypeError):
        pp.PRESETS["x"] = pp.PRESETS["lc29h"]                  # read-only


def test_the_combined_preset_is_opt_in_only():
    assert pp.get_preset("lc29h") is pp.PRESETS["lc29h"]
    with pytest.raises(pp.PresetError, match=r"'lg290p_lc29h' is opt-in .*opt_in=True"):
        pp.get_preset("lg290p_lc29h")
    assert pp.get_preset("lg290p_lc29h", opt_in=True) is pp.PRESETS["lg290p_lc29h"]
    with pytest.raises(pp.PresetError, match=r"unknown preset 'lc29x' \(did you mean 'lc29h'\?\)"):
        pp.get_preset("lc29x")
    assert issubclass(pp.PresetError, ValueError)


def test_preset_info_describes_each_preset():
    lc = pp.preset_info("lc29h")
    assert lc["opt_in"] is False and lc["footprint"] == "Quectel_LC29H" and lc["symbol"] == "LC29H"
    assert "Fig. 19" in lc["source"] and "p. 47" in lc["source"]
    assert "VIHmax 3.08 V" in lc["pin_notes"]["21"]
    combined = pp.preset_info("lg290p_lc29h")
    assert combined["opt_in"] is True and set(combined["conflicts"]) == set(CONFLICTS)
    assert combined["footprints"] == {"lg290p": "Quectel_LG290P_LC29H_StencilLG290P",
                                      "lc29h": "Quectel_LG290P_LC29H_StencilLC29H"}
    assert any("separate footprints" in n for n in combined["notes"])
    assert pp.preset_info("lg290p")["footprint"] == ft.LG290P_NAME
    info = pp.preset_info("lc29h")
    info["notes"].append("mutated")                            # a fresh copy every call
    assert "mutated" not in pp.preset_info("lc29h")["notes"]


# ---------------------------------------------------------------------------
# LC29H
# ---------------------------------------------------------------------------
def test_lc29h_pads_match_fig19():
    pads = by_number(pp.lc29h())
    assert sorted(pads, key=int) == [str(n) for n in range(1, 25)] and len(pp.lc29h().pads) == 24
    for n, (x, y, w, h, shape) in lc29h_expected().items():
        p = pads[n]
        assert (p.x, p.y, p.w, p.h) == pytest.approx((x, y, w, h)), n
        assert p.shape == shape and p.rotation == 0 and p.type == "smd", n
        assert p.pad_layers == ("F.Cu", "F.Mask", "F.Paste"), n
    # LC29H HD V1.3 Fig. 19 (p. 47): pad centre ON the 12.2 mm module edge, 1.25 mm of the
    # 2.5 mm pad beyond it (outer end x = 7.35), pitch 1.1, a 3.0 mm gap after pin 7
    assert pads["1"].x == -6.1 and pads["24"].x == 6.1
    assert pads["1"].x - pads["1"].w / 2 == pytest.approx(-7.35)
    assert pads["1"].x + pads["1"].w / 2 == pytest.approx(-4.85)
    assert pads["2"].y - pads["1"].y == pytest.approx(1.1)
    assert pads["8"].y - pads["7"].y == pytest.approx(3.0)
    # the same y rows as the LG290P edge, pin 1 at y = -7.0
    lg = by_number(ft.lg290p())
    assert all(pads[str(n)].y == lg[str(n)].y for n in range(1, 25))


def test_lc29h_body_courtyard_and_static_checks(tmp_path):
    spec = pp.lc29h()
    # Fig. 20 (p. 52): 12.2 +/- 0.15 x 16.0 +/- 0.15 x 2.5 +/- 0.2 mm
    assert spec.body == ft.Body(12.2, 16.0, w_max=12.35, h_max=16.15, height=2.7)
    # pads (x +/-7.35) and the max body (y +/-8.075) + 0.25 mm, rounded out to 0.01 mm
    assert fa.courtyard_rect(spec) == pytest.approx((-7.6, -8.33, 7.6, 8.33))
    marker = fa.place_pin1_marker(spec)
    assert marker.side == "T" and marker.inside
    path = fa.write_footprint(spec, tmp_path / "SPS_parts.pretty")
    assert path.name == "Quectel_LC29H.kicad_mod"
    assert fa.static_problems(fa.read_footprint(path), path, expected_pads=24) == []
    assert "Fig. 19" in spec.descr and "p. 47" in spec.descr and spec.attr == "smd"


def test_lc29h_forwards_spec_keywords():
    spec = pp.lc29h(descr="custom", model=ft.Model("${KIPRJMOD}/3d/LC29H.step"), pin1=None)
    assert spec.descr == "custom" and spec.model.path.endswith("LC29H.step") and spec.pin1 is None
    assert spec.name == "Quectel_LC29H"


def test_lc29h_symbol_follows_table6():
    spec = pp.lc29h_symbol("SPS_parts:Quectel_LC29H")
    pins = {p.number: (p.name, p.etype) for p in spec.pins}
    assert pins == LC29H_PINS
    assert spec.footprint == "SPS_parts:Quectel_LC29H" and spec.name == "LC29H"
    assert sa.spec_problems(spec) == []
    # 2.8 V I/O domain (RXD1 VIHmax 3.08 V), 1.8 V D_SEL / UART2, VDD_EXT 2.8 V out
    for text in ("2.8 V", "VIHmax 3.08 V", "1.8 V"):
        assert text in spec.description, text
    notes = pp.LC29H_PIN_NOTES
    assert set(notes) == set(LC29H_PINS)
    assert "2.8 V" in notes["21"] and "VIHmax 3.08 V" in notes["21"]
    assert "2.8 V" in notes["7"] and "100 mA" in notes["7"]
    assert "1.8 V" in notes["15"] and "1.8 V" in notes["5"]


def test_lc29h_symbol_authors_with_stacked_ground(tmp_path):
    res = sa.author_symbol(pp.lc29h_symbol(), tmp_path / "SPS_parts.kicad_sym")
    assert res["pins"] == 24 and res["visible_pins"] == 21
    assert res["stacked"] == {"GND": {"visible": "10", "hidden": ["12", "13", "24"]}}
    assert res["pin_groups"]["RESERVED"] == ["2", "4", "17"]


# ---------------------------------------------------------------------------
# LG290P
# ---------------------------------------------------------------------------
def test_lg290p_preset_is_the_f2_author_target():
    fp_fn, sym_fn = pp.PRESETS["lg290p"]
    assert fp_fn() == ft.lg290p() and fp_fn(pin1=None) == ft.lg290p(pin1=None)
    assert sym_fn("L:F") == sa.lg290p_spec("L:F")


@pytest.mark.parametrize("name", sorted(ALL_FOOTPRINTS))
def test_every_preset_pad_number_is_a_symbol_pin(name):
    preset = name.split("-")[0]
    pads = set(by_number(ALL_FOOTPRINTS[name]()))
    pins = {p.number for p in pp.PRESETS[preset].symbol_spec_fn().pins}
    assert pads == pins, sorted(pads ^ pins, key=int)


# ---------------------------------------------------------------------------
# Combined (opt-in)
# ---------------------------------------------------------------------------
def test_combined_union_edge_copper_spans_4_3_to_7_35():
    for stencil in ("lg290p", "lc29h"):
        spec = pp.lg290p_lc29h(stencil=stencil)
        pads = by_number(spec)
        assert sorted(pads, key=int) == [str(n) for n in range(1, 80)]
        lg, lc = by_number(ft.lg290p()), by_number(pp.lc29h())
        for n in range(1, 25):
            p = pads[str(n)]
            x0, _, x1, _ = ft.pad_bbox(p)
            assert sorted((abs(x0), abs(x1))) == pytest.approx([4.3, 7.35]), n
            assert (p.h, p.y, p.shape) == (0.8, lg[str(n)].y, "rect"), n
            assert p.pad_layers == ("F.Cu", "F.Mask"), n                # paste comes separately
            assert covers(ft.pad_bbox(p), ft.pad_bbox(lg[str(n)])), n
            assert covers(ft.pad_bbox(p), ft.pad_bbox(lc[str(n)])), n
        for n in range(25, 80):                                         # the LG290P GND grid
            q, ref = pads[str(n)], lg[str(n)]
            assert (q.x, q.y, q.w, q.h, q.shape) == (ref.x, ref.y, ref.w, ref.h, ref.shape)


def test_combined_lg290p_stencil_pastes_the_lg290p_lands():
    spec = pp.lg290p_lc29h()
    assert spec.name == "Quectel_LG290P_LC29H_StencilLG290P"
    windows = [p for p in paste_windows(spec) if not p.number]
    lands = [p for p in ft.lg290p().pads if int(p.number) <= 24]
    assert sorted((p.x, p.y, p.w, p.h, p.shape) for p in windows) == sorted(
        (p.x, p.y, p.w, p.h, p.shape) for p in lands)
    assert all(p.pad_layers == ("F.Paste",) for p in windows)
    inner = [p for p in spec.pads if p.number and int(p.number) >= 25]
    assert len(inner) == 55 and all("F.Paste" in p.pad_layers for p in inner)


def test_combined_lc29h_stencil_has_no_inner_grid_paste():
    spec = pp.lg290p_lc29h(stencil="lc29h")
    assert spec.name == "Quectel_LG290P_LC29H_StencilLC29H"
    windows = paste_windows(spec)
    assert all(not p.number and p.pad_layers == ("F.Paste",) for p in windows)
    assert sorted((p.x, p.y, p.w, p.h, p.shape) for p in windows) == sorted(
        (x, y, w, h, s) for x, y, w, h, s in lc29h_expected().values())
    # nothing pastes the inner grid: it would sit under an LC29H body with no pads there
    assert all(abs(ft.pad_bbox(p)[0]) >= 4.85 - 1e-9 and abs(ft.pad_bbox(p)[2]) >= 4.85 - 1e-9
               for p in windows)
    inner = [p for p in spec.pads if p.number and int(p.number) >= 25]
    assert len(inner) == 55 and all(p.pad_layers == ("F.Cu", "F.Mask") for p in inner)
    with pytest.raises(ValueError, match=r"stencil 'both' not in \('lg290p', 'lc29h'\)"):
        pp.lg290p_lc29h(stencil="both")


@pytest.mark.parametrize("stencil", ["lg290p", "lc29h"])
def test_combined_courtyard_and_static_checks(stencil, tmp_path):
    spec = pp.lg290p_lc29h(stencil=stencil, descr="d")
    # union copper x +/-7.35 and the LG290P max body y +/-8.15, + 0.25 mm
    assert fa.courtyard_rect(spec) == pytest.approx((-7.6, -8.4, 7.6, 8.4))
    path = fa.write_footprint(spec, tmp_path / "SPS_parts.pretty")
    assert fa.static_problems(fa.read_footprint(path), path, expected_pads=79) == []
    assert spec.descr == "d" and spec.pin1_side == "T"


def test_combined_symbol_puts_the_conflicting_pins_on_no_connect():
    spec = pp.lg290p_lc29h_symbol("L:F")
    base = {p.number: p for p in sa.lg290p_spec("L:F").pins}
    assert spec.name == "LG290P_LC29H" and spec.footprint == "L:F"
    assert sa.spec_problems(spec) == []
    for pin in spec.pins:
        if pin.number in CONFLICTS:
            assert (pin.name, pin.etype) == (CONFLICTS[pin.number], "no_connect"), pin
        else:
            assert pin == base[pin.number], pin
    assert {p.number for p in spec.pins} == set(base)
    assert set(pp.COMBINED_CONFLICTS) == set(CONFLICTS)
    assert "3.08 V" in spec.description and "N/C" in spec.description


# ---------------------------------------------------------------------------
# Netlist hints (F2-S1b): lga_strategy on each preset's own pad table
# ---------------------------------------------------------------------------
LG290P_GND = {"10", "12", "13", *(str(n) for n in range(24, 80))}


def test_presets_carry_lga_netlist_hints():
    assert pp.PRESET_GND_PADS == {"lg290p": LG290P_GND, "lc29h": {"10", "12", "13", "24"},
                                  "lg290p_lc29h": LG290P_GND}
    lg = pp.preset_hints("lg290p")
    assert lg == lga.netlist_hints(ft.lg290p().pads, gnd_pads=sorted(LG290P_GND))
    assert set(lg) == {"preroutes", "notes"} and len(lg["preroutes"]) == 80
    assert {tuple(p["via_size"]) for p in lg["preroutes"][::2]} == {(0.5, 0.25)}
    lc = pp.preset_hints("lc29h")                       # LCC-24: no inner grid, nothing made
    assert set(lc) == {"notes"} and any("no 2x2 ground-pad channel" in n for n in lc["notes"])
    for stencil in pp.STENCILS:                         # the copper is the same in both
        pads = pp.lg290p_lc29h(stencil=stencil).pads
        assert lga.netlist_hints(pads, gnd_pads=LG290P_GND)["preroutes"] == lg["preroutes"]
    assert pp.preset_hints("lg290p_lc29h", opt_in=True)["preroutes"] == lg["preroutes"]
    with pytest.raises(pp.PresetError, match="is opt-in"):
        pp.preset_hints("lg290p_lc29h")


def test_preset_hints_take_strategy_ref_and_fab():
    e3 = pp.preset_hints("lg290p", strategy="e3", ref="U4")
    assert e3["emc"] == {"stitch": True, "max_freq_hz": pp.GNSS_MAX_FREQ_HZ}
    assert {p["path"][0][1] for p in e3["preroutes"]} == {"U4"}
    wide = pp.preset_hints("lg290p", fab={"min_track_width": 0.3})
    assert {p["width"] for p in wide["preroutes"]} == {0.3}
    assert set(pp.preset_hints("lc29h", strategy="b")) == {"planes", "notes"}


def test_preset_hints_place_f2s1s_winning_preroutes_at_u1():
    """F2-S1's winner netlist = the lg290p hints placed at U1 (21, 16)."""
    placed = lga.place_hints(pp.preset_hints("lg290p"), origin=(21.0, 16.0))["preroutes"]
    assert placed[0] == {"net": "GND", "width": 0.25, "layer": "F.Cu",
                         "path": [["pad", "U1", "25"], ["xy", 18.9, 9.7], ["pad", "U1", "37"]],
                         "vias": [[18.9, 9.7]], "via_size": [0.5, 0.25]}
    assert placed[-1] == {"net": "GND", "width": 0.25, "layer": "F.Cu",
                          "path": [["pad", "U1", "78"], ["xy", 23.1, 22.3], ["pad", "U1", "68"]]}


def test_preset_info_carries_the_hints_and_a_strategy_note():
    for name in pp.PRESETS:
        info = pp.preset_info(name)
        assert info["netlist_hints"] == pp.preset_hints(name, opt_in=True), name
        for part in ("F2-S1", "DRC 0 / unconnected 0", "9.0.4", "10.0.5"):
            assert part in info["strategy"], (name, part)
    lg = pp.preset_info("lg290p")["strategy"]
    for part in ("40 GND vias 0.5/0.25", "5 x 11", "12.0 s", "15.3 s", "35.1", "66.8",
                 "apply_hints", "never pour GND on F.Cu"):
        assert part in lg, part
    assert "no inner pad grid" in pp.preset_info("lc29h")["strategy"]
    assert "StencilLC29H" in pp.preset_info("lg290p_lc29h")["strategy"]
    info = pp.preset_info("lg290p")
    info["netlist_hints"]["preroutes"].clear()                     # a fresh copy every call
    assert len(pp.preset_info("lg290p")["netlist_hints"]["preroutes"]) == 80


def test_hints_build_without_sexpdata():
    code = ("import sys; sys.modules['sexpdata'] = None; sys.path.insert(0, sys.argv[1]); "
            "import part_presets as pp, json; print(json.dumps([len(pp.preset_hints("
            "n, opt_in=True).get('preroutes', [])) for n in pp.PRESETS]))")
    r = subprocess.run([sys.executable, "-c", code, str(TOOLS)], capture_output=True,
                       stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == [80, 0, 80]


# ---------------------------------------------------------------------------
# The decision example (module docstring) and the KiCad-python-safe import
# ---------------------------------------------------------------------------
def test_the_docstring_decision_example_runs(tmp_path):
    netlist = tmp_path / "netlist.json"
    tests = doctest.DocTestFinder().find(pp, "part_presets", globs={"netlist": str(netlist)})
    runner = doctest.DocTestRunner(optionflags=doctest.ELLIPSIS)
    for test in tests:
        runner.run(test)
    assert runner.failures == 0 and runner.tries >= 2
    (entry,) = decisions.read_decisions(netlist)
    assert entry["stage"] == "parts" and "Quectel_LC29H" in entry["decision"]
    assert decisions.validate_decision(entry) == []


def test_footprints_build_without_sexpdata():
    code = ("import sys; sys.modules['sexpdata'] = None; sys.path.insert(0, sys.argv[1]); "
            "import part_presets as pp, json; "
            "n = [len(p.footprint_fn().pads) for p in pp.PRESETS.values()]\n"
            "try:\n    pp.PRESETS['lc29h'].symbol_spec_fn(); sym = 'built'\n"
            "except ImportError:\n    sym = 'needs sexpdata'\n"
            "print(json.dumps([n, sym]))")
    r = subprocess.run([sys.executable, "-c", code, str(TOOLS)], capture_output=True,
                       stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == [[79, 24, 24 + 24 + 55],
                                                             "needs sexpdata"]


# ---------------------------------------------------------------------------
# Live tier: KiCad parses and loads every preset footprint and symbol
# ---------------------------------------------------------------------------
def _stable_install() -> env.KiCadInstall | None:
    """The oldest install, if it is a stable release (<= 10): the D0 dialect floor."""
    found = env.discover()
    oldest = min(found, key=lambda i: i.version_tuple) if found else None
    return oldest if oldest and oldest.version_tuple[0] <= 10 else None


@pytest.fixture(scope="module")
def stable() -> env.KiCadInstall:
    inst = _stable_install()
    if inst is None:
        pytest.skip("no KiCad 9/10 install (the D0 dialect floor) to parse / load footprints")
    return inst


@pytest.mark.parametrize("name", sorted(ALL_FOOTPRINTS))
def test_live_oldest_kicad_cli_parses_every_preset_footprint(stable, name, tmp_path):
    path = fa.write_footprint(ALL_FOOTPRINTS[name](), tmp_path / "SPS_parts.pretty")
    assert fa.oldest_kicad_cli()[0] == str(stable.kicad_cli)
    assert fa.kicad_parse_problems(path) == [], stable.version          # the oldest kicad-cli


@pytest.mark.parametrize("name", sorted(ALL_FOOTPRINTS))
def test_live_pcbnew_loads_every_preset_footprint(stable, name, tmp_path):
    if stable.python is None:
        pytest.skip(f"KiCad {stable.version} has no bundled Python with pcbnew")
    spec = ALL_FOOTPRINTS[name]()
    path = fa.write_footprint(spec, tmp_path / "SPS_parts.pretty")
    res = fa.pcbnew_load(path, python=str(stable.python))
    assert res["ok"] and res["loaded"], res
    assert res["pads"] == len(spec.pads) and res["courtyard_outlines"] == 1, res
    assert sorted({n for n in res["numbers"] if n}, key=int) == sorted(by_number(spec), key=int)
    assert res["kicad_version"].startswith(f"{stable.version_tuple[0]}."), res


@pytest.mark.parametrize("preset", sorted(pp.PRESETS))
def test_live_kicad_cli_loads_every_preset_symbol(stable, preset, tmp_path):
    lib = tmp_path / "SPS_parts.kicad_sym"
    sa.author_symbol(pp.PRESETS[preset].symbol_spec_fn(), lib)
    res = sa.validate_library(lib, str(stable.kicad_cli))
    assert res["ok"], res["errors"]


def test_live_kicad9_python_imports_the_presets():
    python = next((i.python for i in env.discover()
                   if i.version_tuple[0] == 9 and i.python is not None), None)
    if python is None:
        pytest.skip("no KiCad 9 bundled Python (3.11) to import part_presets with")
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import part_presets as pp; "
            "print(json.dumps([sys.version_info[:2], "
            "[len(p.footprint_fn().pads) for p in pp.PRESETS.values()], "
            "pp.preset_hints('lg290p')]))")
    r = subprocess.run([str(python), "-c", code, str(TOOLS)], capture_output=True,
                       stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 0, r.stderr
    version, counts, hints = json.loads(r.stdout.strip().splitlines()[-1])
    assert version[:2] >= [3, 11] and counts == [79, 24, 103]
    assert hints == pp.preset_hints("lg290p")
