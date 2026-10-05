"""server/tools/design_blocks.py (compose) and design_paths.relocate_libraries (F46-10).

The base is the Feather-shaped fixture of tests/test_rebuild_variant.py; the
block is a small synthetic GNSS module block (an LG290P-like module, its
decoupling, an active-antenna feed and a PPS test point). Both are synthetic.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import pytest

import design_blocks as db
import design_paths as dp
import lint_netlist
import net_equivalence as ne
import netlist_schema
import rebuild_variant as rv
from test_rebuild_variant import LIB, REFLOW_BOARD, feather_design, write_feather

GLIB = "GNSS_parts"
UART_MAP = {"TXD_EXT": "RX", "RXD": "TX"}


def _pads(spec: str) -> list[list[str]]:
    return [p.split(".") for p in spec.split()]


def gnss_block() -> dict[str, Any]:
    """A fresh GNSS block design (its refs collide with the Feather's)."""
    def part(ref: str, sym: str, value: str, fp: str, **extra: Any) -> dict[str, Any]:
        return {"ref": ref, "lib_id": sym, "value": value, "footprint": fp, **extra}

    return {
        "name": "lg290p_block",
        "board": {"w": 20.0, "h": 15.0},
        "libraries": {"symbols": {GLIB: f"libs/{GLIB}.kicad_sym"},
                      "footprints": {GLIB: "${KIPRJMOD}/libs/GNSS_parts.pretty"}},
        "components": [
            part("U1", f"{GLIB}:LG290P", "LG290P", f"{GLIB}:LG290P_LGA79", x=10.0, y=8.0,
                 rot=90.0),
            part("C1", "Device:C", "10uF", "Capacitor_SMD:C_0603_1608Metric", at=[3.0, 3.0, 0.0]),
            part("C2", "Device:C", "100nF", "Capacitor_SMD:C_0603_1608Metric"),
            part("R1", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric"),
            part("R2", "Device:R", "33R", "Resistor_SMD:R_0603_1608Metric"),
            part("J1", "Connector:Conn_Coaxial", "U.FL",
                 "Connector_Coaxial:U.FL_Hirose_U.FL-R-SMT-1_Vertical"),
            part("L1", "Device:L", "47nH", "Inductor_SMD:L_0603_1608Metric"),
            part("FB1", "Device:FerriteBead", "600R", "Inductor_SMD:L_0603_1608Metric"),
            part("TP1", "Connector:TestPoint", "PPS", "TestPoint:TestPoint_Pad_D1.0mm"),
        ],
        "nets": {name: _pads(spec) for name, spec in {
            "GND": "U1.10 U1.12 U1.13 C1.2 C2.2 J1.2", "+3V3": "U1.5 C1.1 R1.1 FB1.1",
            "V_ANT": "FB1.2 L1.1 C2.1", "RF_IN": "L1.2 J1.1 U1.3", "RESET_N": "R1.2 U1.7",
            "TXD": "U1.20 R2.1", "TXD_EXT": "R2.2", "RXD": "U1.21", "PPS": "U1.30 TP1.1",
        }.items()},
        "no_connect": _pads("U1.40 U1.41"),
        "net_specs": {"RF_IN": {"impedance": 50}, "V_ANT": {"current": 0.05}},
        "power_flags": ["V_ANT"],
        "assumptions": [{"id": "A1", "topic": "antenna", "value": "active antenna",
                         "rationale": "the module needs a 3.3 V bias on the feed",
                         "confidence": "med"}],
    }


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    base_dir, block_dir = tmp_path / "base", tmp_path / "gnss"
    write_feather(base_dir)
    block_dir.mkdir()
    (block_dir / "block.json").write_text(json.dumps(gnss_block()), encoding="utf-8")
    return base_dir, block_dir


def _compose(dirs: tuple[Path, Path], base: dict | None = None, block: dict | None = None,
             **kw: Any) -> db.ComposeResult:
    base_dir, block_dir = dirs
    kw.setdefault("prefix", "GNSS")
    return db.compose(base or feather_design(), block or gnss_block(), base_dir=base_dir,
                      block_dir=block_dir, **kw)


def _union(base: dict, block: dict, ref_map: dict, net_map: dict,
           drop: tuple[str, ...] = ()) -> dict[str, set]:
    out = {n: {(r, str(p)) for r, p in pads if r not in drop} for n, pads in base["nets"].items()}
    for name, pads in block["nets"].items():
        out.setdefault(net_map[name], set()).update((ref_map[r], str(p)) for r, p in pads)
    return {n: pads for n, pads in out.items() if pads}


def _comp(design: dict, ref: str) -> dict:
    return next(c for c in design["components"] if c["ref"] == ref)


# --- the acceptance criterion -------------------------------------------------------------
def test_compose_feather_and_gnss(dirs: tuple[Path, Path]) -> None:
    base, block = feather_design(), gnss_block()
    before = (copy.deepcopy(base), copy.deepcopy(block))
    res = _compose(dirs, base, block, net_map=UART_MAP, join_nets=("GND", "+3V3"))
    assert (base, block) == before                       # inputs never mutated
    out = res.design
    assert res.ref_map == {"U1": "U5", "C1": "C15", "C2": "C16", "R1": "R10", "R2": "R11",
                           "J1": "J6", "L1": "L1", "FB1": "FB2", "TP1": "TP1"}
    refs = [c["ref"] for c in out["components"]]
    assert len(refs) == len(set(refs)) == 44 + 9
    assert res.net_map == {"GND": "GND", "+3V3": "+3V3", "V_ANT": "GNSS_V_ANT",
                           "RF_IN": "GNSS_RF_IN", "RESET_N": "GNSS_RESET_N", "TXD": "GNSS_TXD",
                           "TXD_EXT": "RX", "RXD": "TX", "PPS": "GNSS_PPS"}
    assert ["U5", "10"] in out["nets"]["GND"] and ["U1", "10"] in out["nets"]["GND"]
    assert out["libraries"] == {
        "symbols": {LIB: f"libs/{LIB}.kicad_sym", GLIB: f"../gnss/libs/{GLIB}.kicad_sym"},
        "footprints": {LIB: f"libs/{LIB}.pretty",
                       GLIB: "${KIPRJMOD}/../gnss/libs/GNSS_parts.pretty"}}
    assert out["blocks"] == [{"name": "GNSS", "design": "lg290p_block", "source": None,
                              "rebuild": None, "ref_map": res.ref_map, "net_map": res.net_map,
                              "drop_refs": [], "board": {"w": 20.0, "h": 15.0}}]
    assert out["source"] == base["source"] and out["board"] == base["board"]
    assert out["net_specs"]["GNSS_RF_IN"] == {"impedance": 50}
    assert out["power_flags"] == ["GNSS_V_ANT"]
    assert [a["id"] for a in out["assumptions"]] == ["A1", "GNSS-A1"]
    assert ["U5", "40"] in out["no_connect"] and ["J1", "4"] in out["no_connect"]
    u5 = _comp(out, "U5")
    assert "x" not in u5 and "y" not in u5 and u5["rot"] == 90.0 and "at" not in _comp(out, "C15")
    assert "at" in _comp(out, "U1")                      # base placement untouched
    assert res.problems == ()
    assert netlist_schema.validate(out) == []
    assert lint_netlist.lint(out)["errors"] == []
    eq = ne.compare(ne.netlist_pad_nets(out), _union(base, block, res.ref_map, res.net_map))
    assert eq.equal, eq.summary()
    json.dumps(res.as_dict())


def test_default_join_is_gnd_only(dirs: tuple[Path, Path]) -> None:
    res = _compose(dirs)
    assert res.net_map["GND"] == "GND" and res.net_map["+3V3"] == "GNSS_+3V3"
    assert res.net_map["TXD_EXT"] == "GNSS_TXD_EXT"


def test_private_nets_keep_and_collisions(dirs: tuple[Path, Path]) -> None:
    block = gnss_block()
    block["nets"] = {("RESET" if n == "RESET_N" else n): p for n, p in block["nets"].items()}
    res = _compose(dirs, block=block, private_nets="keep", net_map=UART_MAP)
    assert res.net_map["V_ANT"] == "V_ANT" and res.net_map["RESET"] == "GNSS_RESET"
    assert any("RESET" in p and "collides" in p for p in res.problems)
    base = feather_design()
    base["nets"]["GNSS_PPS"] = base["nets"].pop("D12")
    taken = _compose(dirs, base=base, net_map={**UART_MAP, "TXD": "GNSS_RF_IN"})
    assert taken.net_map["PPS"] == "GNSS_PPS_2" and taken.net_map["RF_IN"] == "GNSS_RF_IN_2"
    assert len([p for p in taken.problems if "GNSS_PPS" in p or "GNSS_RF_IN" in p]) == 2


def test_join_net_missing_in_base(dirs: tuple[Path, Path]) -> None:
    res = _compose(dirs, join_nets=["GND", "V_ANT"], net_map=UART_MAP)
    assert res.net_map["V_ANT"] == "V_ANT"
    assert any("V_ANT" in p and "not a base net" in p for p in res.problems)


def test_explicit_ref_map_and_renumbering(dirs: tuple[Path, Path]) -> None:
    res = _compose(dirs, ref_map={"U1": "U10", "TP1": "TP7"}, net_map=UART_MAP)
    assert res.ref_map["U1"] == "U10" and res.ref_map["TP1"] == "TP7"
    assert res.ref_map["C1"] == "C15"
    base = {"components": [{"ref": "MK", "footprint": "A:B", "board_only": True},
                           {"ref": "R1", "lib_id": "D:R", "footprint": "A:R"}], "nets": {}}
    block = {"components": [{"ref": r, "lib_id": "D:R", "footprint": "A:R"}
                            for r in ("MK", "R1", "R2")],
             "nets": {"N": [["R1", "1"], ["R2", "1"]]}}
    small = _compose(dirs, base=base, block=block, prefix="B")
    assert small.ref_map == {"MK": "MK1", "R1": "R3", "R2": "R2"}
    assert small.net_map == {"N": "B_N"}
    assert small.design["nets"] == {"B_N": [["R3", "1"], ["R2", "1"]]}


@pytest.mark.parametrize(("ref_map", "match"), [
    ({"Q9": "Q1"}, "not a block ref"), ({"C1": "C3"}, "base"),
    ({"C1": "C40", "C2": "C40"}, "both"), ({"C1": ""}, "empty"),
    ({"C1": "L1"}, "block ref"),
])
def test_bad_ref_map(dirs: tuple[Path, Path], ref_map: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _compose(dirs, ref_map=ref_map)


@pytest.mark.parametrize(("kw", "exc", "match"), [
    ({"prefix": ""}, ValueError, "prefix"), ({"prefix": "GN SS"}, ValueError, "prefix"),
    ({"private_nets": "hide"}, ValueError, "private_nets"),
    ({"ref_map": ["U1"]}, TypeError, "ref_map"), ({"join_nets": "GND"}, TypeError, "join_nets"),
    ({"drop_refs": "J1"}, TypeError, "drop_refs"), ({"drop_refs": ["Q99"]}, ValueError, "Q99"),
    ({"net_map": {"NOPE": "X"}}, ValueError, "NOPE"), ({"net_map": {"PPS": ""}}, ValueError, "PPS"),
    ({"net_map": ["x"]}, TypeError, "net_map"),
])
def test_bad_arguments(dirs: tuple[Path, Path], kw: dict, exc: type, match: str) -> None:
    with pytest.raises(exc, match=match):
        _compose(dirs, **kw)


def test_bad_designs(dirs: tuple[Path, Path]) -> None:
    with pytest.raises(ValueError, match="components"):
        _compose(dirs, block={"components": "x"})
    with pytest.raises(ValueError, match="nets"):
        _compose(dirs, block={"components": [], "nets": []})
    with pytest.raises(TypeError, match="join_nets"):
        _compose(dirs, join_nets=["GND", 1])


def test_odd_block_records(dirs: tuple[Path, Path]) -> None:
    block = gnss_block()
    block["components"].append(dict(block["components"][-1]))           # TP1 twice
    block["net_specs"]["GHOST"] = {"current": 1.0}
    block["libraries"]["footprints"]["Stock"] = "${KICAD9_FOOTPRINT_DIR}/Stock.pretty"
    base = feather_design()
    base["libraries"]["footprints"]["Stock"] = "${KICAD9_FOOTPRINT_DIR}/Stock.pretty"
    res = _compose(dirs, base=base, block=block, net_map=UART_MAP)
    text = "\n".join(res.problems)
    assert "duplicate refs" in text and "GHOST" in text and "Stock" not in text
    assert "GHOST" not in res.design["net_specs"]


def test_drop_refs_replaces_a_base_connector(dirs: tuple[Path, Path]) -> None:
    base, block = feather_design(), gnss_block()
    res = _compose(dirs, base, block, net_map={**UART_MAP, "PPS": "USB_DP"},
                   join_nets=("GND", "+3V3"), drop_refs=["J1", "FB1"])
    out = res.design
    assert "J1" not in [c["ref"] for c in out["components"]]
    assert res.ref_map["J1"] == "J6" and res.ref_map["FB1"] == "FB2"
    assert "VBUS_RAW" not in out["nets"]
    assert ["U1", "33"] in out["no_connect"] and ["J1", "4"] not in out["no_connect"]
    assert sorted(map(tuple, out["nets"]["USB_DP"])) == [("TP1", "1"), ("U1", "34"), ("U5", "30")]
    assert out["blocks"][0]["drop_refs"] == ["J1", "FB1"]
    assert any("VBUS_RAW" in p for p in res.problems)
    assert any("USB_DM" in p and "no_connect" in p for p in res.problems)
    assert [a["topic"] for a in out["assumptions"]].count("no_connect") == 1
    assert netlist_schema.validate(out) == [] and lint_netlist.lint(out)["errors"] == []
    union = _union(base, block, res.ref_map, res.net_map, drop=("J1", "FB1"))
    eq = ne.compare(ne.netlist_pad_nets(out), union)
    assert eq.equal, eq.summary()


def test_compose_onto_a_reflow_variant_never_reuses_dropped_refs(
        dirs: tuple[Path, Path]) -> None:
    base_dir, _block_dir = dirs
    variant = rv.make_variant(feather_design(), base_dir, mode="reflow", board=REFLOW_BOARD)
    assert variant.ok and "H1" in variant.design["variant"]["dropped_refs"]
    block = gnss_block()
    block["components"].append({"ref": "H1", "board_only": True, "value": "H1",
                                "footprint": "MountingHole:MountingHole_2.2mm_M2"})
    res = _compose(dirs, base=variant.design, block=block, net_map=UART_MAP)
    assert res.ref_map["H1"] == "H3" and res.problems == ()
    assert netlist_schema.validate(res.design) == []
    assert lint_netlist.lint(res.design)["errors"] == []
    with pytest.raises(ValueError, match="reuses H2"):
        _compose(dirs, base=variant.design, block=block, net_map=UART_MAP, ref_map={"H1": "H2"})


def test_chained_compose_never_reuses_dropped_refs(dirs: tuple[Path, Path]) -> None:
    first = _compose(dirs, net_map=UART_MAP, drop_refs=["J1"])
    assert first.ref_map["J1"] == "J6"
    second = _compose(dirs, base=first.design, prefix="GNSS2", net_map=UART_MAP)
    assert second.ref_map["J1"] == "J7"
    assert "J1" not in [c["ref"] for c in second.design["components"]]
    with pytest.raises(ValueError, match="reuses J1"):
        _compose(dirs, base=first.design, prefix="GNSS2", net_map=UART_MAP,
                 ref_map={"J1": "J1"})
    with pytest.raises(ValueError, match="reuses J1"):
        _compose(dirs, net_map=UART_MAP, drop_refs=["J1"], ref_map={"J1": "J1"})


@pytest.mark.parametrize("key", ["nets", "no_connect"])
def test_block_pads_must_name_block_parts(dirs: tuple[Path, Path], key: str) -> None:
    block = gnss_block()
    if key == "nets":
        block["nets"]["PPS"] = [*block["nets"]["PPS"], ["U3", "1"]]   # U3 is a base part only
    else:
        block["no_connect"] = [*block["no_connect"], ["U3", "2"]]
    with pytest.raises(ValueError, match=f"block's {key}.*U3"):
        _compose(dirs, block=block, net_map=UART_MAP)


def test_drop_refs_removes_specs_of_vanished_nets(dirs: tuple[Path, Path]) -> None:
    base = feather_design()
    base["power_flags"] = ["ANT", "GND"]
    base["assumptions"].append({"id": "compose-GNSS-1", "topic": "t", "value": "v",
                                "rationale": "r", "confidence": "low"})
    res = _compose(dirs, base=base, net_map=UART_MAP, drop_refs=["U2", "J5"])
    assert "ANT" not in res.design["nets"] and "ANT" not in res.design["net_specs"]
    assert res.design["power_flags"] == ["GND", "GNSS_V_ANT"]
    assert any("ANT" in p and "net_specs" in p for p in res.problems)
    ids = [a["id"] for a in res.design["assumptions"]]
    assert len(ids) == len(set(ids)) and "compose-GNSS-2" in ids


def test_net_specs_merge(dirs: tuple[Path, Path]) -> None:
    block = gnss_block()
    block["nets"]["VBUS"] = [["R2", "3"], ["C1", "3"]]
    block["nets"]["DM"] = [["J1", "3"], ["U1", "50"]]
    block["net_specs"] = {"VBUS": {"voltage": 5.0, "current": 1.0}, "GND": {"current": 2.0},
                          "TXD": {"diff_pair": "DM"}}
    res = _compose(dirs, block=block, join_nets=("GND", "VBUS"), net_map=UART_MAP)
    specs = res.design["net_specs"]
    assert specs["VBUS"] == {"voltage": 5.0, "current": 0.5}       # the base wins
    assert specs["GND"] == {"current": 2.0}
    assert specs["GNSS_TXD"] == {"diff_pair": "GNSS_DM"}
    assert any("net_specs" in p and "VBUS" in p for p in res.problems)


def test_libraries_conflict_and_same_library(dirs: tuple[Path, Path]) -> None:
    base_dir, _block_dir = dirs
    block = gnss_block()
    block["libraries"]["footprints"][LIB] = "libs/other.pretty"
    res = _compose(dirs, block=block, net_map=UART_MAP)
    assert res.design["libraries"]["footprints"][LIB] == f"libs/{LIB}.pretty"
    assert any(LIB in p and "different" in p for p in res.problems)
    same = gnss_block()
    same["libraries"] = {"footprints": {LIB: f"${{KIPRJMOD}}/libs/{LIB}.pretty"}}
    ok = db.compose(feather_design(), same, base_dir=base_dir, block_dir=base_dir,
                    prefix="GNSS", net_map=UART_MAP)
    assert ok.problems == () and "symbols" in ok.design["libraries"]
    bare = {k: v for k, v in feather_design().items() if k != "libraries"}
    merged = db.compose(bare, gnss_block(), base_dir=base_dir, block_dir=base_dir,
                        prefix="G", net_map=UART_MAP)
    assert merged.design["libraries"]["symbols"] == {GLIB: f"libs/{GLIB}.kicad_sym"}


def test_ignored_block_keys(dirs: tuple[Path, Path]) -> None:
    block = {**gnss_block(), "copper_layers": 4, "planes": {"In1.Cu": "GND"},
             "rebuild": "reflow", "mode": "napkin", "title": "x"}
    base = {**feather_design(), "mode": "napkin"}
    res = _compose(dirs, base=base, block=block, net_map=UART_MAP)
    text = "\n".join(res.problems)
    assert "copper_layers" in text and "planes" in text and "'mode'" not in text
    assert res.design["copper_layers"] == 2 and "planes" not in res.design
    assert res.design["blocks"][0]["rebuild"] == "reflow"


def test_block_provenance_and_nesting(dirs: tuple[Path, Path]) -> None:
    block = {**gnss_block(),
             "source": {"kind": "kicad", "path": "orig/gnss.kicad_pro", "reference": "ref.json"},
             "variant": {"mode": "reflow", "dropped_refs": ["H1"]},
             "blocks": [{"name": "LNA", "source": None, "rebuild": None,
                         "ref_map": {"Q1": "L1"}, "net_map": {"IN": "RF_IN"}, "drop_refs": []}]}
    base = {**feather_design(), "blocks": [{"name": "OLD", "ref_map": {"X1": "X1"}}]}
    res = _compose(dirs, base=base, block=block, net_map=UART_MAP)
    blocks = res.design["blocks"]
    assert [b["name"] for b in blocks] == ["OLD", "GNSS", "GNSS/LNA"]
    assert blocks[1]["source"]["path"] == "../gnss/orig/gnss.kicad_pro"
    assert blocks[1]["source"]["reference"] == "../gnss/ref.json"
    assert blocks[1]["variant"] == block["variant"]
    assert blocks[2]["ref_map"] == {"Q1": "L1"} and blocks[2]["net_map"] == {"IN": "GNSS_RF_IN"}
    assert netlist_schema.validate(res.design) == []


def test_keep_placement(dirs: tuple[Path, Path]) -> None:
    res = _compose(dirs, keep_placement=True, net_map=UART_MAP)
    assert _comp(res.design, "U5")["x"] == 10.0 and _comp(res.design, "C15")["at"]


@pytest.mark.parametrize(("who", "key", "value"), [
    ("block", "libraries", "x"), ("block", "libraries", {"footprints": {GLIB: 3}}),
    ("base", "libraries", {"footprints": "libs"}), ("block", "libraries", {"models": {}}),
    ("block", "libraries", {"footprints": {"": "libs/x.pretty"}}),
    ("block", "nets", {"N": [["R1"]]}), ("block", "nets", {"N": "R1.1"}),
    ("block", "nets", {"N": [["R1", True]]}), ("base", "no_connect", [["J1", "4", "x"]]),
    ("block", "net_specs", {"RF_IN": 50}), ("block", "power_flags", "V_ANT"),
    ("block", "assumptions", {"id": "A1"}), ("base", "blocks", [{"name": "OLD",
                                                                 "ref_map": ["X1"]}]),
    ("block", "blocks", ["LNA"]), ("block", "assumptions", [{"topic": "antenna"}]),
    ("base", "assumptions", [{"id": ""}]),
    ("base", "variant", {"mode": "reflow", "dropped_refs": "H1"}),
    ("base", "variant", ["reflow"]), ("base", "blocks", [{"name": "OLD", "drop_refs": "J1"}]),
])
def test_malformed_records_raise(dirs: tuple[Path, Path], who: str, key: str,
                                 value: object) -> None:
    base, block = feather_design(), gnss_block()
    (base if who == "base" else block)[key] = value
    with pytest.raises(ValueError, match=f"the {who}.*'{key}'"):
        _compose(dirs, base=base, block=block)


def test_prefix_reuse_and_assumption_ids(dirs: tuple[Path, Path]) -> None:
    first = _compose(dirs, net_map=UART_MAP)
    with pytest.raises(ValueError, match="GNSS_2"):
        _compose(dirs, base=first.design, net_map=UART_MAP)
    second = _compose(dirs, base=first.design, prefix="GNSS2", net_map=UART_MAP)
    assert [b["name"] for b in second.design["blocks"]] == ["GNSS", "GNSS2"]
    ids = [a["id"] for a in second.design["assumptions"]]
    assert ids == ["A1", "GNSS-A1", "GNSS2-A1"]
    nested = {**feather_design(), "blocks": [{"name": "RF/LNA", "ref_map": {"Q1": "Q1"}}]}
    with pytest.raises(ValueError, match="'RF'"):
        _compose(dirs, base=nested, prefix="RF", net_map=UART_MAP)
    base = feather_design()
    base["assumptions"].append({"id": "GNSS-A1", "topic": "t", "value": "v", "rationale": "r",
                                "confidence": "low"})
    block = gnss_block()
    block["assumptions"].append({**block["assumptions"][0]})                  # A1 twice
    res = _compose(dirs, base=base, block=block, net_map=UART_MAP)
    ids = [a["id"] for a in res.design["assumptions"]]
    assert ids == ["A1", "GNSS-A1", "GNSS-A1_2", "GNSS-A1_3"]
    assert len([p for p in res.problems if "GNSS-A1" in p and "taken" in p]) == 2
    assert netlist_schema.validate(res.design) == []


def test_backslash_paths_name_one_library(dirs: tuple[Path, Path], tmp_path: Path) -> None:
    block = gnss_block()
    block["libraries"]["footprints"][LIB] = f"..\\base\\libs\\{LIB}.pretty"
    block["libraries"]["symbols"][LIB] = f"$(KIPRJMOD)\\..\\base\\libs\\{LIB}.kicad_sym"
    res = _compose(dirs, block=block, net_map=UART_MAP)
    assert res.problems == ()
    base_dir, _block_dir = dirs
    same = {**gnss_block(), "libraries": {"footprints": {LIB: f"libs\\{LIB}.pretty"}}}
    ok = db.compose(feather_design(), same, base_dir=base_dir, block_dir=base_dir,
                    prefix="G", net_map=UART_MAP)
    assert ok.problems == ()
    assert dp.resolve_path("libs\\a\\b", tmp_path) == tmp_path / "libs" / "a" / "b"
    assert dp.resolve_path("${KIPRJMOD}\\libs\\a", tmp_path) == tmp_path / "libs" / "a"
    assert dp.relocate_path("libs\\a\\b.pretty", tmp_path / "x", tmp_path / "y") == \
        "../x/libs/a/b.pretty"
    assert dp.relocate_path("C:\\abs\\a.pretty", tmp_path, tmp_path / "y") == "C:\\abs\\a.pretty"


# --- relocate_libraries -------------------------------------------------------------------
def test_relocate_libraries(tmp_path: Path) -> None:
    src, dst = tmp_path / "a" / "import", tmp_path / "a" / "reflow" / "v1"
    design = {
        "libraries": {"symbols": {"S": "libs/S.kicad_sym", "A": "C:/abs/A.kicad_sym",
                                  "P": "/opt/P.kicad_sym"},
                      "footprints": {"K": "${KIPRJMOD}/libs/K.pretty",
                                     "M": "$(KIPRJMOD)\\libs\\M.pretty",
                                     "E": "${KICAD9_FOOTPRINT_DIR}/E.pretty",
                                     "R": "${KIPRJMOD}"}},
        "source": {"kind": "kicad", "path": "orig/x.kicad_pro", "reference": "ref.json",
                   "erc_baseline": "erc.json", "drc_baseline": "drc.json",
                   "import_report": "${KIPRJMOD}/import_report.json", "via": "_f/x.kicad_pro",
                   "board": "orig/x.kicad_pcb", "files": {"x.kicad_pro": "ab" * 32}},
        "variant": {"mode": "reflow", "from": "netlist.json"},
        "board": {"outline": {"dxf": "mech/o.dxf"}},
        "blocks": [{"name": "B", "ref_map": {"a": "a"}, "source": {"kind": "kicad",
                                                                   "path": "b/b.kicad_pro"},
                    "board": {"outline": {"dxf": "b/mech/b.dxf"}},
                    "variant": {"mode": "reflow", "from": "b/netlist.json"}},
                   {"name": "C", "ref_map": {"a": "a"}, "source": None, "board": {"w": 5},
                    "variant": {"mode": "keep"}}],
        "components": [],
    }
    before = copy.deepcopy(design)
    out = dp.relocate_libraries(design, src, dst)
    assert design == before
    assert out["libraries"]["symbols"] == {"S": "../../import/libs/S.kicad_sym",
                                           "A": "C:/abs/A.kicad_sym", "P": "/opt/P.kicad_sym"}
    assert out["libraries"]["footprints"] == {
        "K": "${KIPRJMOD}/../../import/libs/K.pretty",
        "M": "$(KIPRJMOD)/../../import/libs/M.pretty",
        "E": "${KICAD9_FOOTPRINT_DIR}/E.pretty", "R": "${KIPRJMOD}/../../import"}
    s = out["source"]
    assert (s["path"], s["reference"], s["erc_baseline"], s["drc_baseline"]) == (
        "../../import/orig/x.kicad_pro", "../../import/ref.json", "../../import/erc.json",
        "../../import/drc.json")
    assert s["import_report"] == "${KIPRJMOD}/../../import/import_report.json"
    assert s["via"] == "../../import/_f/x.kicad_pro" and s["files"] == before["source"]["files"]
    assert out["variant"]["from"] == "../../import/netlist.json"
    assert out["board"]["outline"]["dxf"] == "../../import/mech/o.dxf"
    assert out["blocks"][0]["source"]["path"] == "../../import/b/b.kicad_pro"
    assert out["blocks"][0]["board"]["outline"]["dxf"] == "../../import/b/mech/b.dxf"
    assert out["blocks"][0]["variant"]["from"] == "../../import/b/netlist.json"
    assert out["blocks"][1] == before["blocks"][1]
    assert dp.relocate_libraries(design, src, src)["libraries"] == design["libraries"] | {
        "footprints": {**design["libraries"]["footprints"], "M": "$(KIPRJMOD)/libs/M.pretty",
                       "R": "${KIPRJMOD}"}}
    assert dp.relocate_libraries({"components": []}, src, dst) == {"components": []}
    odd = {"libraries": "x", "blocks": "y", "source": None, "board": None, "components": []}
    assert dp.relocate_libraries(odd, src, dst) == odd


def test_relocate_path_edge_cases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert dp.relocate_path("", tmp_path, tmp_path / "x") == ""
    assert dp.relocate_path(None, tmp_path, tmp_path / "x") is None      # type: ignore[arg-type]
    assert dp.relocate_path("${KIPRJMOD}/${X}/a", tmp_path, tmp_path / "x") == "${KIPRJMOD}/${X}/a"

    def other_drive(*_a: Any) -> str:
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr(os.path, "relpath", other_drive)
    moved = dp.relocate_path("libs/a.pretty", tmp_path, "D:/elsewhere")
    assert moved == (tmp_path / "libs" / "a.pretty").as_posix()


def test_resolve_path(tmp_path: Path) -> None:
    assert dp.resolve_path("libs/a", tmp_path) == tmp_path / "libs" / "a"
    assert dp.resolve_path("${KIPRJMOD}/libs/a", tmp_path) == tmp_path / "libs" / "a"
    assert dp.resolve_path("${KICAD9_FOOTPRINT_DIR}/a", tmp_path) is None
    assert dp.resolve_path(str(tmp_path / "z"), "elsewhere") == tmp_path / "z"


# --- CLI ----------------------------------------------------------------------------------
def test_cli_compose(dirs: tuple[Path, Path], tmp_path: Path,
                     capsys: pytest.CaptureFixture) -> None:
    base_dir, block_dir = dirs
    out = tmp_path / "composite" / "netlist.json"
    net_map = tmp_path / "net_map.json"
    net_map.write_text(json.dumps(UART_MAP), encoding="utf-8")
    rc = db.main([str(base_dir / "netlist.json"), str(block_dir / "block.json"), str(out),
                  "--prefix", "GNSS", "--net-map", str(net_map), "--join-nets", "GND,+3V3",
                  "--ref-map", json.dumps({"U1": "U9"})])
    assert rc == 0, capsys.readouterr()
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True and report["ref_map"]["U1"] == "U9"
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["libraries"]["footprints"][LIB] == f"../base/libs/{LIB}.pretty"
    assert written["libraries"]["footprints"][GLIB] == \
        "${KIPRJMOD}/../gnss/libs/GNSS_parts.pretty"
    assert written["source"]["reference"] == "../base/reference_padnets.json"
    assert dp.footprint_file(written, out.parent, f"{LIB}:Hole_2.5mm_NPTH") is not None
    rep = lint_netlist.lint(written, ctx=lint_netlist.cli_context(out))
    assert rep["errors"] == [] and not [w for w in rep["warnings"] if w.startswith("IM-")]


def test_cli_relocates_block_provenance(dirs: tuple[Path, Path], tmp_path: Path,
                                        capsys: pytest.CaptureFixture) -> None:
    base_dir, block_dir = dirs
    block = {**gnss_block(), "board": {"outline": {"dxf": "mech/gnss.dxf"}},
             "variant": {"mode": "reflow", "from": "orig/netlist.json"},
             "source": {"kind": "kicad", "path": "orig/gnss.kicad_pro"}}
    (block_dir / "block.json").write_text(json.dumps(block), encoding="utf-8")
    out = tmp_path / "deep" / "c2" / "netlist.json"
    rc = db.main([str(base_dir / "netlist.json"), str(block_dir / "block.json"), str(out),
                  "--prefix", "GNSS", "--net-map", json.dumps(UART_MAP)])
    assert rc == 0, capsys.readouterr()
    entry = json.loads(out.read_text(encoding="utf-8"))["blocks"][0]
    assert entry["source"]["path"] == "../../gnss/orig/gnss.kicad_pro"
    assert entry["board"]["outline"]["dxf"] == "../../gnss/mech/gnss.dxf"
    assert entry["variant"]["from"] == "../../gnss/orig/netlist.json"


def test_cli_compose_problems_and_flags(dirs: tuple[Path, Path], tmp_path: Path,
                                        capsys: pytest.CaptureFixture) -> None:
    base_dir, block_dir = dirs
    out = tmp_path / "c2.json"
    rc = db.main([str(base_dir / "netlist.json"), str(block_dir / "block.json"), str(out),
                  "--prefix", "GNSS", "--net-map", json.dumps(UART_MAP), "--drop-refs", "J1",
                  "--keep-nets", "--keep-placement"])
    assert rc == 1 and out.is_file()
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False and report["problems"]
    written = json.loads(out.read_text(encoding="utf-8"))
    assert _comp(written, "U5")["x"] == 10.0 and "V_ANT" in written["nets"]


def test_cli_compose_errors(dirs: tuple[Path, Path], tmp_path: Path,
                            capsys: pytest.CaptureFixture) -> None:
    base_dir, block_dir = dirs
    base, block = str(base_dir / "netlist.json"), str(block_dir / "block.json")
    assert db.main([base, block, base, "--prefix", "G"]) == 2
    assert "overwrite" in capsys.readouterr().err
    assert db.main([base, str(tmp_path / "none.json"), str(tmp_path / "o.json"),
                    "--prefix", "G"]) == 2
    assert db.main([base, block, str(tmp_path / "o.json"), "--prefix", "G",
                    "--net-map", "{bad"]) == 2
    assert db.main([base, block, str(tmp_path / "o.json"), "--prefix", "G",
                    "--drop-refs", "Q99"]) == 2
    listed = tmp_path / "list.json"
    listed.write_text("[]", encoding="utf-8")
    assert db.main([base, str(listed), str(tmp_path / "o.json"), "--prefix", "G"]) == 2
    odd = tmp_path / "odd.json"
    odd.write_text(json.dumps({**gnss_block(), "libraries": "libs"}), encoding="utf-8")
    capsys.readouterr()
    assert db.main([base, str(odd), str(tmp_path / "o.json"), "--prefix", "G"]) == 2
    assert "'libraries'" in capsys.readouterr().err
    assert not (tmp_path / "o.json").exists()


def test_cli_map_files_with_bom(dirs: tuple[Path, Path], tmp_path: Path,
                                capsys: pytest.CaptureFixture) -> None:
    base_dir, block_dir = dirs
    net_map, ref_map = tmp_path / "net_map.json", tmp_path / "ref_map.json"
    net_map.write_text(json.dumps(UART_MAP), encoding="utf-8-sig")       # PowerShell 5 Out-File
    ref_map.write_text(json.dumps({"U1": "U9"}), encoding="utf-8-sig")
    out = tmp_path / "bom" / "netlist.json"
    rc = db.main([str(base_dir / "netlist.json"), str(block_dir / "block.json"), str(out),
                  "--prefix", "GNSS", "--net-map", str(net_map), "--ref-map", str(ref_map)])
    assert rc == 0, capsys.readouterr()
    report = json.loads(capsys.readouterr().out)
    assert report["ref_map"]["U1"] == "U9" and report["net_map"]["RXD"] == "TX"
