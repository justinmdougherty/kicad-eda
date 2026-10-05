"""sym_author — KiCad symbol authoring from a pin table (F2, ROADMAP D1).

Unit tier: the LG290P pin table (Quectel LG290P(03) Hardware Design V1.0,
Fig. 3 / Table 6), GND stacking, layout/grid rules, spec validation, the
add-or-replace library writer, and the consumers that must load the result
(symlib.flatten, build_pin_sidecars, sch_build) — no KiCad needed.

Live tier (skipped without kicad-cli): KiCad parses the written library
(``sym upgrade`` into a temp dir), and KiCad's ERC + netlist export treat the
hidden stacked GND pins as connected to the visible one.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import pytest
import sexpdata

import build_pin_sidecars
import sch_build
import sym_author
import symlib
from fixtures.kicad_install import KICAD, requires_kicad_cli

S = symlib.S
GRID = 2.54
NICK = "SPS_parts"
LIB_ID = f"{NICK}:LG290P"

# Quectel LG290P(03) Hardware Design V1.0, Table 6 (GND and RESERVED below)
LG290P_SIGNALS = {
    "23": ("VCC", "power_in"),
    "22": ("V_BCKP", "power_in"),
    "9": ("VDD_RF", "power_out"),
    "11": ("RF_IN", "input"),
    "8": ("RESET_N", "input"),
    "4": ("EVENT", "input"),
    "20": ("TXD1", "output"),
    "21": ("RXD1", "input"),
    "6": ("TXD2", "output"),
    "7": ("RXD2", "input"),
    "16": ("TXD3", "output"),
    "15": ("RXD3", "input"),
    "18": ("I2C_SDA", "bidirectional"),
    "19": ("I2C_SCL", "input"),
    "3": ("1PPS", "output"),
    "14": ("RTK_STAT/ANT_ON", "output"),
}
LG290P_RESERVED = ["1", "2", "5", "17"]
LG290P_GND = ["10", "12", "13"] + [str(n) for n in range(24, 80)]


# --- helpers -------------------------------------------------------------------

def _write_lg290p(lib_dir: Path, footprint: str = "") -> tuple[Path, dict]:
    lib = lib_dir / f"{NICK}.kicad_sym"
    return lib, sym_author.author_symbol(sym_author.lg290p_spec(footprint), lib)


def _flat(lib: Path, name: str = "LG290P") -> list:
    return symlib.flatten({}, lib.parent, lib.stem, name)


def _pins(flat: list) -> dict[str, dict]:
    """{number: {name, etype, at: (x, y, rot), hidden}} from a flattened symbol."""
    out = {}
    for sub in symlib.kids(flat, "symbol"):
        for pin in symlib.kids(sub, "pin"):
            at = symlib.first(pin, "at")
            hide = symlib.first(pin, "hide")
            out[symlib.first(pin, "number")[1]] = {
                "name": symlib.first(pin, "name")[1],
                "etype": pin[1].value(),
                "at": (float(at[1]), float(at[2]), int(at[3])),
                "hidden": hide is not None and hide[1] == S("yes"),
            }
    return out


def _body(flat: list) -> tuple[float, float, float, float]:
    for sub in symlib.kids(flat, "symbol"):
        rect = symlib.first(sub, "rectangle")
        if rect is not None:
            (_, x0, y0), (_, x1, y1) = symlib.first(rect, "start"), symlib.first(rect, "end")
            return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
    raise AssertionError("symbol has no body rectangle")


def _props(flat: list) -> dict[str, str]:
    return {p[1]: p[2] for p in symlib.kids(flat, "property")}


def _on_grid(v: float) -> bool:
    return abs(v / GRID - round(v / GRID)) < 1e-6


def _small_spec(**kw) -> sym_author.SymbolSpec:
    P = sym_author.PinSpec
    pins = (P("1", "VCC", "power_in"), P("2", "IN", "input"), P("3", "OUT", "output"),
            P("4", "GND", "power_in"), P("5", "GND", "power_in"), P("6", "VCC", "power_in"),
            P("7", "NC", "no_connect"), P("8", "NC", "no_connect"), P("9", "GND", "power_in"))
    return sym_author.SymbolSpec(name=kw.pop("name", "SMALL"), pins=pins, **kw)


# --- the LG290P pin table --------------------------------------------------------

def test_lg290p_spec_has_79_pins_numbered_1_to_79():
    spec = sym_author.lg290p_spec()
    numbers = [p.number for p in spec.pins]
    assert len(numbers) == 79 and len(set(numbers)) == 79
    assert set(numbers) == {str(n) for n in range(1, 80)}


def test_lg290p_spec_matches_the_datasheet_pin_table():
    by_num = {p.number: p for p in sym_author.lg290p_spec().pins}
    for num, (name, etype) in LG290P_SIGNALS.items():
        assert (by_num[num].name, by_num[num].etype) == (name, etype), num
    for num in LG290P_RESERVED:
        assert (by_num[num].name, by_num[num].etype) == ("RESERVED", "no_connect"), num
    gnd = [p.number for p in sym_author.lg290p_spec().pins if p.name == "GND"]
    assert gnd == LG290P_GND and len(gnd) == 59
    assert all(by_num[n].etype == "power_in" for n in gnd)


def test_lg290p_pin_groups_name_every_gnd_and_reserved_pad():
    groups = sym_author.pin_groups(sym_author.lg290p_spec())
    assert groups == {"GND": LG290P_GND, "RESERVED": LG290P_RESERVED}
    assert groups["GND"][0] == "10"       # the visible pin of the stack comes first


def test_author_lg290p_result_summary(tmp_path: Path):
    lib, res = _write_lg290p(tmp_path, footprint="SPS_parts:Quectel_LG290P")
    assert res["ok"] is True and res["replaced"] is False
    assert res["library"] == str(lib) and lib.is_file()
    assert res["nickname"] == NICK and res["lib_id"] == LIB_ID
    assert res["pins"] == 79 and res["visible_pins"] == 79 - 58
    assert res["pin_groups"] == {"GND": LG290P_GND, "RESERVED": LG290P_RESERVED}
    assert res["stacked"] == {"GND": {"visible": "10", "hidden": LG290P_GND[1:]}}
    assert res["format_version"] == 20241209
    w, h = res["body_mm"]
    assert _on_grid(w) and _on_grid(h) and w > 0 and h > 0


def test_written_library_is_the_d0_dialect(tmp_path: Path):
    lib, _ = _write_lg290p(tmp_path)
    root = sexpdata.loads(lib.read_text(encoding="utf-8"))
    assert symlib.is_(root, "kicad_symbol_lib")
    assert symlib.first(root, "version") == [S("version"), 20241209]
    assert symlib.first(root, "generator")[1] == "kicad-eda"
    assert [s[1] for s in symlib.kids(root, "symbol")] == ["LG290P"]


# --- symlib loads it; pins, types and GND stacking ----------------------------------

def test_symlib_flatten_loads_lg290p_with_79_typed_pins(tmp_path: Path):
    lib, _ = _write_lg290p(tmp_path)
    flat = _flat(lib)
    assert len(symlib.pin_numbers(flat)) == 79
    etypes = symlib.pin_etypes(flat)
    for num, (_name, etype) in LG290P_SIGNALS.items():
        assert etypes[num] == etype, num
    assert all(etypes[n] == "no_connect" for n in LG290P_RESERVED)
    assert etypes["10"] == "power_in"
    # hidden stacked GND pins are passive (KLC S4.3, as KiCad 9's RF_GPS:L70-R)
    assert all(etypes[n] == "passive" for n in LG290P_GND[1:])
    assert symlib.unit_pins(flat) == {1: symlib.pin_numbers(flat)}


def test_gnd_pins_beyond_the_first_are_stacked_and_hidden(tmp_path: Path):
    lib, _ = _write_lg290p(tmp_path)
    pins = _pins(_flat(lib))
    first = pins["10"]
    assert first["name"] == "GND" and not first["hidden"]
    for num in LG290P_GND[1:]:
        assert pins[num]["name"] == "GND", num
        assert pins[num]["hidden"], num
        assert pins[num]["at"] == first["at"], num
    visible = [n for n, p in pins.items() if not p["hidden"]]
    assert len(visible) == 21
    # every visible pin has its own connection point
    spots = [pins[n]["at"][:2] for n in visible]
    assert len(set(spots)) == len(spots)


def test_pin_offsets_resolve_gnd_by_name_to_the_visible_pin(tmp_path: Path):
    lib, _ = _write_lg290p(tmp_path)
    off = symlib.pin_offsets(_flat(lib))
    assert off["GND"]["number"] == "10"
    assert all((off[n]["x"], off[n]["y"]) == (off["10"]["x"], off["10"]["y"])
               for n in LG290P_GND)


def test_lg290p_layout_power_top_ground_bottom_signals_on_the_sides(tmp_path: Path):
    lib, _ = _write_lg290p(tmp_path)
    flat = _flat(lib)
    x0, y0, x1, y1 = _body(flat)
    pins = _pins(flat)
    for num in ("23", "22", "9"):                    # power: top, pointing down
        x, y, rot = pins[num]["at"]
        assert y == pytest.approx(y1 + 2.54) and rot == 270 and x0 < x < x1, num
    x, y, rot = pins["10"]["at"]                     # ground: bottom, pointing up
    assert y == pytest.approx(y0 - 2.54) and rot == 90 and x0 < x < x1
    for num in ("11", "8", "4", *LG290P_RESERVED):   # inputs / RF / reserved: left
        x, y, rot = pins[num]["at"]
        assert x == pytest.approx(x0 - 2.54) and rot == 0 and y0 < y < y1, num
    for num in ("20", "21", "6", "7", "16", "15", "18", "19", "3", "14"):
        x, y, rot = pins[num]["at"]                  # interfaces / status: right
        assert x == pytest.approx(x1 + 2.54) and rot == 180 and y0 < y < y1, num


def test_lg290p_signal_groups_are_contiguous_and_separated(tmp_path: Path):
    lib, _ = _write_lg290p(tmp_path)
    pins = _pins(_flat(lib))
    y = {n: pins[n]["at"][1] for n in pins}
    # a group's pins sit on adjacent grid rows ...
    for a, b in (("20", "21"), ("6", "7"), ("16", "15"), ("18", "19"), ("3", "14"),
                 ("8", "4"), ("1", "2"), ("2", "5"), ("5", "17")):
        assert y[a] - y[b] == pytest.approx(GRID), (a, b)
    # ... and one empty row separates neighbouring groups
    for a, b in (("21", "6"), ("7", "16"), ("15", "18"), ("19", "3"), ("11", "8"), ("4", "1")):
        assert y[a] - y[b] == pytest.approx(2 * GRID), (a, b)


def test_every_pin_and_the_body_sit_on_the_100mil_grid(tmp_path: Path):
    lib, _ = _write_lg290p(tmp_path)
    flat = _flat(lib)
    assert all(_on_grid(v) for v in _body(flat))
    for num, pin in _pins(flat).items():
        assert _on_grid(pin["at"][0]) and _on_grid(pin["at"][1]), num


def test_lg290p_properties(tmp_path: Path):
    lib, _ = _write_lg290p(tmp_path, footprint="SPS_parts:Quectel_LG290P")
    props = _props(_flat(lib))
    assert props["Reference"] == "U"
    assert props["Value"] == "LG290P"
    assert props["Footprint"] == "SPS_parts:Quectel_LG290P"
    assert props["Datasheet"].startswith("https://") and props["Datasheet"].endswith(".pdf")
    assert "LG290P" in props["Description"] and "LGA" in props["Description"]
    assert "GNSS" in props["ki_keywords"].split()
    assert props["ki_fp_filters"] == "*LG290P*"


# --- layout rules on a generic spec ---------------------------------------------------

def test_layout_estimated_pin_name_text_never_overlaps():
    """Left/right names stay out of the top/bottom pin band and away from each
    other; top and bottom names do not meet (CHAR_W is calibrated against
    kicad-cli 9 SVG output, so the estimate is conservative)."""
    for spec in (sym_author.lg290p_spec(), _small_spec()):
        lay = sym_author.layout(spec)
        vis = [p for p in lay.pins if not p.hidden]
        w2, h2 = lay.half_w, lay.half_h
        text = {p.number: sym_author.text_length(p.name) for p in vis}
        side = {p.number: p.side for p in vis}
        left_end = max((-w2 + sym_author.NAME_OFFSET + text[n]
                        for n in text if side[n] == "left"), default=-w2)
        right_start = min((w2 - sym_author.NAME_OFFSET - text[n]
                           for n in text if side[n] == "right"), default=w2)
        assert left_end < right_start, spec.name
        for p in vis:
            if p.side in ("top", "bottom"):
                assert left_end < p.x - sym_author.FONT / 2, (spec.name, p.number)
                assert p.x + sym_author.FONT / 2 < right_start, (spec.name, p.number)
        top = max((text[n] for n in text if side[n] == "top"), default=0.0)
        bottom = max((text[n] for n in text if side[n] == "bottom"), default=0.0)
        assert 2 * h2 > top + bottom + 2 * sym_author.NAME_OFFSET, spec.name


def test_auto_sides_follow_the_electrical_type():
    lay = sym_author.layout(_small_spec())
    side = {p.number: p.side for p in lay.pins}
    assert side["1"] == "top" and side["4"] == "bottom"
    assert side["2"] == "left" and side["3"] == "right" and side["7"] == "left"


def test_generic_power_stacks_and_pin_groups():
    spec = _small_spec()
    assert sym_author.pin_groups(spec) == {"VCC": ["1", "6"], "GND": ["4", "5", "9"],
                                           "NC": ["7", "8"]}
    lay = sym_author.layout(spec)
    assert lay.stacks == {"VCC": ("1", "6"), "GND": ("4", "5", "9")}
    by_num = {p.number: p for p in lay.pins}
    assert [n for n, p in by_num.items() if p.hidden] == ["5", "6", "9"]
    assert (by_num["6"].x, by_num["6"].y) == (by_num["1"].x, by_num["1"].y)
    assert by_num["6"].etype == "passive" and by_num["1"].etype == "power_in"
    # no_connect duplicates are NOT stacked: each reserved pad stays open on its own
    assert not by_num["7"].hidden and not by_num["8"].hidden
    assert (by_num["7"].x, by_num["7"].y) != (by_num["8"].x, by_num["8"].y)


def test_stack_power_off_keeps_every_pin_visible():
    lay = sym_author.layout(_small_spec(stack_power=False))
    assert lay.stacks == {} and not any(p.hidden for p in lay.pins)
    spots = [(p.x, p.y) for p in lay.pins]
    assert len(set(spots)) == len(spots)


@pytest.mark.parametrize("name,etype,side", [
    ("AGND", "power_in", "bottom"), ("VSSA", "power_in", "bottom"),
    ("GND", "passive", "bottom"),          # e.g. an exposed pad typed passive
    ("VDDIO", "power_out", "top"), ("GNDSENSE", "input", "left"),
    ("SDA", "open_collector", "right"), ("X", "unspecified", "left"),
])
def test_default_side_by_type_and_ground_name(name: str, etype: str, side: str):
    spec = sym_author.SymbolSpec(name="X", pins=(sym_author.PinSpec("1", name, etype),))
    assert sym_author.layout(spec).pins[0].side == side


def test_explicit_side_wins_over_the_type_default():
    P = sym_author.PinSpec
    spec = sym_author.SymbolSpec(name="X", pins=(P("1", "A", "input", side="right"),
                                                 P("2", "B", "output", side="left")))
    side = {p.number: p.side for p in sym_author.layout(spec).pins}
    assert side == {"1": "right", "2": "left"}


def test_text_length_ignores_overbar_markup_and_anonymous_names():
    assert sym_author.text_length("~{RESET}") == pytest.approx(5 * sym_author.CHAR_W)
    assert sym_author.text_length("~") == 0.0
    assert sym_author.text_length("GND") == pytest.approx(3 * sym_author.CHAR_W)


# --- spec validation and the JSON boundary ---------------------------------------

@pytest.mark.parametrize("pins,match", [
    ((), "at least one pin"),
    ((("1", "A", "input"), ("1", "B", "input")), "duplicate pin number '1'"),
    ((("1", "A", "inputs"),), "electrical type 'inputs'"),
    ((("", "A", "input"),), "pin number"),
    ((("1 2", "A", "input"),), "pin number"),
    ((("1", "", "input"),), "pin name"),
])
def test_invalid_pin_tables_are_rejected(pins: tuple, match: str):
    spec = sym_author.SymbolSpec(name="X", pins=tuple(sym_author.PinSpec(*p) for p in pins))
    with pytest.raises(sym_author.SymbolAuthorError, match=match):
        sym_author.layout(spec)


def test_invalid_side_name_and_reference_are_rejected():
    P = sym_author.PinSpec
    with pytest.raises(sym_author.SymbolAuthorError, match="side 'up'"):
        sym_author.layout(sym_author.SymbolSpec(name="X", pins=(P("1", "A", "input", "up"),)))
    with pytest.raises(sym_author.SymbolAuthorError, match="symbol name"):
        sym_author.layout(sym_author.SymbolSpec(name="A:B", pins=(P("1", "A"),)))
    with pytest.raises(sym_author.SymbolAuthorError, match="reference prefix"):
        sym_author.layout(sym_author.SymbolSpec(name="X", pins=(P("1", "A"),), reference="U1"))


def test_validation_reports_every_problem_at_once():
    P = sym_author.PinSpec
    spec = sym_author.SymbolSpec(name="X", pins=(P("1", "A", "bogus"), P("1", "B", "input", "up")))
    with pytest.raises(sym_author.SymbolAuthorError) as err:
        sym_author.layout(spec)
    msg = str(err.value)
    assert "bogus" in msg and "duplicate pin number" in msg and "side 'up'" in msg


def test_spec_from_dict_builds_the_spec_and_accepts_integer_pin_numbers():
    spec = sym_author.spec_from_dict({
        "name": "DEMO", "reference": "U", "footprint": "Lib:FP", "keywords": "demo part",
        "pins": [{"number": 1, "name": "VCC", "type": "power_in", "side": "top"},
                 {"number": "2", "name": "IO", "type": "bidirectional", "group": "BUS"}],
    })
    assert spec.name == "DEMO" and spec.footprint == "Lib:FP" and spec.keywords == "demo part"
    assert spec.pins == (sym_author.PinSpec("1", "VCC", "power_in", "top", ""),
                         sym_author.PinSpec("2", "IO", "bidirectional", "", "BUS"))
    assert spec.stack_power is True


@pytest.mark.parametrize("data,match", [
    ({"name": "X", "pins": [], "colour": "red"}, "unknown symbol key"),
    ({"name": "X", "pins": [{"number": "1", "name": "A", "kind": "input"}]}, "unknown pin key"),
    ({"name": "X", "pins": "1,2"}, "'pins' must be a list"),
    ({"pins": [{"number": "1", "name": "A"}]}, "'name' is required"),
    ({"name": "X", "pins": [{"name": "A"}]}, "'number' is required"),
    ({"name": "X", "pins": [{"number": 1.5, "name": "A"}]}, "must be a string"),
    ({"name": "X", "pins": [], "stack_power": "yes"}, "'stack_power' must be a boolean"),
    ("not a dict", "must be an object"),
])
def test_spec_from_dict_rejects_malformed_input(data, match: str):
    with pytest.raises(sym_author.SymbolAuthorError, match=match):
        sym_author.spec_from_dict(data)


# --- the library writer: add or replace, dialect guard ------------------------------

def test_add_then_replace_keeps_the_other_symbols(tmp_path: Path):
    lib = tmp_path / "Mix_parts.kicad_sym"
    sym_author.author_symbol(_small_spec(name="AAA"), lib)
    res_b = sym_author.author_symbol(_small_spec(name="BBB", value="first"), lib)
    assert res_b["replaced"] is False
    res = sym_author.author_symbol(_small_spec(name="AAA", value="second"), lib)
    assert res["replaced"] is True
    syms = symlib.load_lib(tmp_path, "Mix_parts")
    assert list(syms) == ["AAA", "BBB"]                       # order kept, replaced in place
    assert _props(syms["AAA"])["Value"] == "second"
    assert _props(syms["BBB"])["Value"] == "first"
    assert len(symlib.pin_numbers(symlib.flatten({}, tmp_path, "Mix_parts", "BBB"))) == 9


def test_rewrite_preserves_foreign_symbols_byte_for_meaning(tmp_path: Path):
    """A symbol the tool did not write (here: a KiCad 9 stock-style one with
    escapes and an `extends` child) survives a rewrite unchanged as data."""
    lib = tmp_path / "Own_parts.kicad_sym"
    lib.write_text(
        '(kicad_symbol_lib (version 20241209) (generator "kicad_symbol_editor")'
        ' (generator_version "9.0")'
        ' (symbol "R1" (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))'
        '  (property "Value" "a \\"quoted\\" 1/4\\\\W" (at 0 0 0) (effects (font (size 1.27 1.27))))'
        '  (symbol "R1_1_1" (pin passive line (at 0 3.81 270) (length 1.27)'
        '   (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))))'
        ' (symbol "R2" (extends "R1")'
        '  (property "Value" "R2" (at 0 0 0) (effects (font (size 1.27 1.27))))))\n',
        encoding="utf-8")
    before = sexpdata.loads(lib.read_text(encoding="utf-8"), nil=None, true=None)
    sym_author.author_symbol(_small_spec(name="NEW"), lib)
    after = sexpdata.loads(lib.read_text(encoding="utf-8"), nil=None, true=None)
    assert symlib.kids(after, "symbol")[:2] == symlib.kids(before, "symbol")
    assert symlib.first(after, "generator") == symlib.first(before, "generator")
    assert symlib.flatten({}, tmp_path, "Own_parts", "R2")[1] == "R2"
    assert _props(symlib.load_lib(tmp_path, "Own_parts")["R1"])["Value"] == 'a "quoted" 1/4\\W'


@pytest.mark.parametrize("version", [20250114, 20231120])
def test_refuses_to_extend_a_library_in_another_dialect(tmp_path: Path, version: int):
    lib = tmp_path / "Other.kicad_sym"
    text = f'(kicad_symbol_lib (version {version}) (generator "x"))\n'
    lib.write_text(text, encoding="utf-8")
    with pytest.raises(sym_author.SymbolAuthorError, match=f"format {version}.*sym upgrade"):
        sym_author.author_symbol(_small_spec(), lib)
    assert lib.read_text(encoding="utf-8") == text             # untouched


@pytest.mark.parametrize("text,match", [
    ('(kicad_symbol_lib (version 20241209)', "cannot parse"),
    ('(kicad_pcb (version 20241229))', "not a KiCad symbol library"),
])
def test_refuses_unparseable_or_foreign_files(tmp_path: Path, text: str, match: str):
    lib = tmp_path / "Bad.kicad_sym"
    lib.write_text(text, encoding="utf-8")
    with pytest.raises(sym_author.SymbolAuthorError, match=match):
        sym_author.author_symbol(_small_spec(), lib)


def test_refuses_a_path_without_the_kicad_sym_suffix(tmp_path: Path):
    with pytest.raises(sym_author.SymbolAuthorError, match=r"\.kicad_sym"):
        sym_author.author_symbol(_small_spec(), tmp_path / "parts.lib")


def test_creates_missing_parent_directories(tmp_path: Path):
    lib = tmp_path / "libs" / "deep" / "New_parts.kicad_sym"
    assert sym_author.author_symbol(_small_spec(), lib)["ok"]
    assert lib.is_file()


def test_strings_with_quotes_and_backslashes_round_trip(tmp_path: Path):
    lib = tmp_path / "Q_parts.kicad_sym"
    desc = 'He said "hi" \\ bye'
    sym_author.author_symbol(_small_spec(description=desc), lib)
    assert _props(_flat(lib, "SMALL"))["Description"] == desc


def test_library_text_is_kicad_style_and_round_trips():
    text = sym_author.library_text([sym_author.symbol_node(_small_spec())])
    assert text.startswith("(kicad_symbol_lib\n\t(version 20241209)\n")
    assert "\t\t(pin_names\n\t\t\t(offset 1.016)\n\t\t)" in text
    root = sexpdata.loads(text, nil=None, true=None)
    again = sexpdata.loads(sym_author.library_text(symlib.kids(root, "symbol")), nil=None, true=None)
    assert again == root


def test_number_formatting_has_no_exponents_or_negative_zero():
    assert sym_author.fmt_number(-0.0) == "0"
    assert sym_author.fmt_number(2.54) == "2.54"
    assert sym_author.fmt_number(1e-05) == "0.00001"
    assert sym_author.fmt_number(12.700000000000001) == "12.7"
    assert sym_author.fmt_number(3) == "3"
    with pytest.raises(sym_author.SymbolAuthorError, match="boolean"):
        sym_author.fmt_number(True)


def test_unwritable_atoms_fail_loudly():
    with pytest.raises(sym_author.SymbolAuthorError, match="cannot write NoneType"):
        sym_author.library_text([[S("symbol"), "X", [S("at"), None]]])
    assert sym_author.library_text([[S("symbol"), "X", []]]).count("()") == 1


def test_refuses_a_directory_named_like_a_library(tmp_path: Path):
    (tmp_path / "Dir.kicad_sym").mkdir()
    with pytest.raises(sym_author.SymbolAuthorError, match="not a file"):
        sym_author.author_symbol(_small_spec(), tmp_path / "Dir.kicad_sym")


def test_author_symbol_accepts_a_json_shaped_spec(tmp_path: Path):
    res = sym_author.author_symbol(
        {"name": "J", "pins": [{"number": "1", "name": "A", "type": "passive"}]},
        tmp_path / "J_parts.kicad_sym")
    assert res["lib_id"] == "J_parts:J" and res["pins"] == 1 and res["pin_groups"] == {}


def test_validate_library_reports_a_kicad_cli_that_cannot_start(tmp_path: Path):
    lib, _ = _write_lg290p(tmp_path)
    res = sym_author.validate_library(lib, str(tmp_path / "no-such-kicad-cli.exe"))
    assert res["ok"] is False
    assert any("did not run" in e for e in res["errors"]), res


def _fake_cli(monkeypatch, console: str, write_output: bool = True) -> None:
    """Stand-in for `kicad-cli sym upgrade -o OUT IN`: rc 0, copies IN to OUT."""
    def run(args, **_kw):
        if write_output:
            shutil.copyfile(args[-1], args[args.index("-o") + 1])
        return subprocess.CompletedProcess(args, 0, stdout=console, stderr="")
    monkeypatch.setattr(sym_author.subprocess, "run", run)


@pytest.mark.parametrize("console,write_output,ok", [
    ("Saving symbol library C:/work/error_logs/SPS_parts.kicad_sym\n", True, True),
    ("Error: unknown token 'bogus'\n", True, False),      # D0: rc 0 is not proof
    ("", False, False),                                    # rc 0 but nothing written
])
def test_validate_library_judges_the_console_and_the_output_not_just_rc(
        tmp_path: Path, monkeypatch, console: str, write_output: bool, ok: bool):
    lib, _ = _write_lg290p(tmp_path)
    _fake_cli(monkeypatch, console, write_output)
    res = sym_author.validate_library(lib, "kicad-cli")
    assert res["ok"] is ok, res
    assert res["resaved_pins"] == ({"LG290P": 79} if ok else {})


def test_validate_library_flags_a_resave_that_drops_pins(tmp_path: Path, monkeypatch):
    lib, _ = _write_lg290p(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    sym_author.author_symbol(_small_spec(name="LG290P"), other / lib.name)

    def run(args, **_kw):
        shutil.copyfile(other / lib.name, args[args.index("-o") + 1])
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    monkeypatch.setattr(sym_author.subprocess, "run", run)
    res = sym_author.validate_library(lib, "kicad-cli")
    assert res["ok"] is False
    assert any("different pin set" in e for e in res["errors"]), res


def test_validate_library_reports_a_library_symlib_cannot_load(tmp_path: Path):
    lib = tmp_path / "Bad_parts.kicad_sym"
    lib.write_text("(kicad_symbol_lib (symbol", encoding="utf-8")
    res = sym_author.validate_library(lib, None)
    assert res["ok"] is False and res["symlib_pins"] == {}
    assert any(e.startswith("symlib: cannot parse") for e in res["errors"]), res


# --- command line ------------------------------------------------------------------

def test_cli_writes_the_lg290p_and_prints_the_summary_last(tmp_path: Path, capsys):
    lib = tmp_path / "SPS_parts.kicad_sym"
    assert sym_author.main(["--lg290p", str(lib), "SPS_parts:Quectel_LG290P"]) == 0
    res = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert res["ok"] and res["pins"] == 79 and lib.is_file()
    assert _props(_flat(lib))["Footprint"] == "SPS_parts:Quectel_LG290P"


def test_cli_spec_file_errors_are_json_and_exit_1(tmp_path: Path, capsys):
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"name": "X", "pins": []}), encoding="utf-8")
    assert sym_author.main([str(spec), str(tmp_path / "X.kicad_sym")]) == 1
    res = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert res["ok"] is False and "at least one pin" in res["error"]


def test_cli_usage_exit_2(capsys):
    assert sym_author.main([]) == 2
    assert "Usage" in capsys.readouterr().out


# --- consumers: pin sidecar and the file schematic backend ------------------------

def test_build_pin_sidecars_writes_an_lg290p_sidecar_into_a_temp_dir(tmp_path: Path):
    lib_dir, pins_dir = tmp_path / "libs", tmp_path / "pins"
    lib_dir.mkdir()
    pins_dir.mkdir()
    _write_lg290p(lib_dir)
    summary = build_pin_sidecars.build_all(lib_dir, pins_dir, [LIB_ID])
    assert summary["ok"] and summary["failed"] == []
    path = pins_dir / f"{NICK}_LG290P.pins.json"
    assert summary["written"] == [str(path)]
    sidecar = json.loads(path.read_text(encoding="utf-8"))
    assert {str(n) for n in range(1, 80)} <= set(sidecar)
    assert sidecar["GND"] == sidecar["10"]
    assert all(sidecar[n] == sidecar["10"] for n in LG290P_GND)
    assert sidecar["RF_IN"] == sidecar["11"]


def test_write_pin_sidecar_supports_a_nickname_other_than_the_file_stem(tmp_path: Path):
    lib, _ = _write_lg290p(tmp_path / "libs")
    res = sym_author.write_pin_sidecar(lib, "LG290P", tmp_path / "pins", nickname="GNSS")
    path = tmp_path / "pins" / "GNSS_LG290P.pins.json"
    assert res == {"ok": True, "sidecar": str(path), "lib_id": "GNSS:LG290P",
                   "keys": len(json.loads(path.read_text(encoding="utf-8")))}
    with pytest.raises(sym_author.SymbolAuthorError, match="NOPE"):
        sym_author.write_pin_sidecar(lib, "NOPE", tmp_path / "pins")


def _lg290p_design(gnd_pads: list[str], gnd_net: str = "GND") -> dict:
    """One LG290P with its supplies netted and every other signal pin flagged
    no_connect (RESERVED pins are typed no_connect and need no flag). No
    footprint: footprint libraries are not under test here."""
    others = [n for n in LG290P_SIGNALS if n not in ("23", "22")]
    return {
        "name": "lg290p_stack",
        "components": [{"ref": "U1", "lib_id": LIB_ID, "value": "LG290P", "footprint": ""}],
        "nets": {gnd_net: [["U1", p] for p in gnd_pads],
                 "VCC_3V3": [["U1", "23"], ["U1", "22"]]},
        "no_connect": [["U1", n] for n in others],
    }


def _project(tmp_path: Path, design: dict, power_lib: Path) -> Path:
    """A schematic project dir built by the repo's file backend (sch_build)."""
    _write_lg290p(tmp_path)
    shutil.copyfile(power_lib, tmp_path / "power.kicad_sym")
    (tmp_path / "netlist.json").write_text(json.dumps(design), encoding="utf-8")
    sch = tmp_path / "lg290p_stack.kicad_sch"
    res = sch_build.build(str(tmp_path / "netlist.json"), str(sch), str(tmp_path))
    assert res["ok"], res
    return sch


def test_sch_build_places_the_lg290p_from_the_authored_library(tmp_path: Path):
    power = Path(__file__).resolve().parent / "fixtures" / "symbols" / "power.kicad_sym"
    sch = _project(tmp_path, _lg290p_design(LG290P_GND), power)
    text = sch.read_text(encoding="utf-8")
    assert f'(symbol "{LIB_ID}"' in text
    spots = Counter(ln.split("(at ")[1].split(")")[0] for ln in text.splitlines()
                    if ln.lstrip().startswith('(label "GND"'))
    # 59 pad labels on the one stacked connection point, plus the PWR_FLAG's own
    assert sorted(spots.values()) == [1, 59]


# --- live tier: KiCad itself --------------------------------------------------------

@requires_kicad_cli
def test_kicad_cli_sym_upgrade_parses_the_lg290p_library(tmp_path: Path):
    lib, _ = _write_lg290p(tmp_path / "libs")
    res = sym_author.validate_library(lib, str(KICAD.cli))
    assert res["ok"], res
    assert res["errors"] == []
    assert res["symlib_pins"] == {"LG290P": 79}
    assert res["resaved_pins"] == {"LG290P": 79}
    assert res["resaved_hidden_pins"] == {"LG290P": 58}
    assert res["kicad_cli"] == str(KICAD.cli)


@requires_kicad_cli
def test_validate_library_reports_a_library_kicad_cannot_load(tmp_path: Path):
    lib = tmp_path / "Broken_parts.kicad_sym"
    lib.write_text('(kicad_symbol_lib (version 20241209) (generator "x")\n'
                   ' (symbol "B" (pin bogus_type line (at 0 0 0) (length 2.54)'
                   ' (name "A") (number "1"))))\n', encoding="utf-8")
    res = sym_author.validate_library(lib, str(KICAD.cli))
    assert res["ok"] is False
    assert any(e.startswith("kicad-cli") for e in res["errors"]), res


def test_validate_library_without_kicad_cli_is_a_failure_not_a_pass(tmp_path: Path):
    lib, _ = _write_lg290p(tmp_path)
    res = sym_author.validate_library(lib, None)
    assert res["ok"] is False and res["symlib_pins"] == {"LG290P": 79}
    assert any("kicad-cli not available" in e for e in res["errors"])


def _kicad_project_files(sch: Path) -> None:
    """Project tables + .kicad_pro so ERC resolves both libraries locally (D0)."""
    (sch.parent / "sym-lib-table").write_text(
        "(sym_lib_table\n  (version 7)\n"
        f'  (lib (name "{NICK}")(type "KiCad")(uri "${{KIPRJMOD}}/{NICK}.kicad_sym")'
        '(options "")(descr ""))\n'
        '  (lib (name "power")(type "KiCad")(uri "${KIPRJMOD}/power.kicad_sym")'
        '(options "")(descr ""))\n)\n', encoding="utf-8")
    pro = {"meta": {"filename": sch.with_suffix(".kicad_pro").name, "version": 1}}
    sch.with_suffix(".kicad_pro").write_text(json.dumps(pro), encoding="utf-8")


def _kicad(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([str(KICAD.cli), *args], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=300, check=False)


def _u1_pins_by_net(sch: Path) -> dict[str, set[str]]:
    xml = sch.with_suffix(".xml")
    r = _kicad(["sch", "export", "netlist", "--format", "kicadxml", "-o", str(xml), str(sch)])
    assert r.returncode == 0 and xml.is_file(), r.stdout + r.stderr
    out: dict[str, set[str]] = {}
    for net in ET.parse(xml).getroot().iter("net"):
        pins = {n.get("pin") for n in net.iter("node") if n.get("ref") == "U1"}
        if pins:
            out[net.get("name").lstrip("/")] = pins
    return out


@requires_kicad_cli
@pytest.mark.parametrize("gnd_pads,gnd_net", [(LG290P_GND, "GND"), (["10"], "GND"),
                                              (["10"], "GND_RF")],
                         ids=["all_gnd_pads", "first_gnd_pad_only", "renamed_ground_net"])
def test_kicad_erc_and_netlist_treat_stacked_gnd_pins_as_connected(tmp_path: Path,
                                                                   gnd_pads: list[str],
                                                                   gnd_net: str):
    """KiCad's own connectivity: whether the netlist lists all 59 GND pads
    (pin_groups) or only the visible pad 10, every stacked GND pad lands on
    the ground net, and ERC raises nothing (no unconnected hidden pin, no
    undriven power pin, embedded symbol identical to the library).

    renamed_ground_net guards the passive typing of the hidden pins: hidden
    power_in pins act as an implicit global "GND" net, so KiCad 9 would move
    all 59 pads to "GND" and warn multiple_net_names (verified by hand)."""
    sch = _project(tmp_path, _lg290p_design(gnd_pads, gnd_net),
                   KICAD.symbols / "power.kicad_sym")
    _kicad_project_files(sch)
    rpt = tmp_path / "erc.json"
    r = _kicad(["sch", "erc", "--format", "json", "-o", str(rpt), str(sch)])
    assert rpt.is_file(), r.stdout + r.stderr
    violations = [(v["type"], v["severity"], v["description"])
                  for s in json.loads(rpt.read_text(encoding="utf-8"))["sheets"]
                  for v in s["violations"]]
    assert violations == [], violations
    nets = _u1_pins_by_net(sch)
    assert nets[gnd_net] == set(LG290P_GND)
    assert nets["VCC_3V3"] == {"23", "22"}
