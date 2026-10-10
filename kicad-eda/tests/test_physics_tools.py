"""physics_tools (D7-13a): the five 2a physics MCP tools and their {ok, reason} envelopes."""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

import decisions
import impedance
import physics_resolve as pr
import physics_tools as pt
import stackup_model
import tool_registry
from conftest import load_example

ROOT = Path(__file__).resolve().parents[1]
NAMES = ("resolve_stackup", "impedance_calc", "list_materials", "list_stackup_presets",
         "check_layer_count")


def _netlist(tmp_path: Path, design: dict, name: str = "netlist.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(design), encoding="utf-8")
    return str(path)


def _ok(out: dict) -> dict:
    assert isinstance(out, dict) and out.get("ok") is True, out.get("reason")
    json.dumps(out)                                           # MCP returns JSON
    return out


def _refused(out: dict, *words: str) -> None:
    assert isinstance(out, dict) and out.get("ok") is False, out
    assert isinstance(out.get("reason"), str) and out["reason"].strip()
    for word in words:
        assert word in out["reason"], out["reason"]
    json.dumps(out)


# ---------------------------------------------------------------- registry
def test_tools_tuple_holds_the_five_2a_tools_in_order() -> None:
    assert tuple(fn.__name__ for fn in pt.TOOLS) == NAMES
    assert "TOOLS" in pt.__doc__ and "ROADMAP_DECISIONS" in pt.__doc__
    for fn in pt.TOOLS:
        assert fn.__doc__ and fn.__doc__.strip(), fn.__name__


def test_the_tools_are_served_and_callable_over_mcp() -> None:
    server = FastMCP("physics-probe")
    names = tool_registry.register_tool_modules(server, (pt,))
    assert tuple(names) == NAMES
    result = asyncio.run(server.call_tool("list_materials", {"query": "RO4350B"}))
    blocks = result[0] if isinstance(result, tuple) else result
    out = json.loads(blocks[0].text)
    assert out["ok"] and [m["id"] for m in out["materials"]] == ["rogers.ro4350b"]


# ---------------------------------------------------------------- resolve_stackup
def test_resolve_stackup_writes_the_artifact_and_dru(tmp_path: Path) -> None:
    net = _netlist(tmp_path, load_example("rf_coupon_50r"))
    out = _ok(pt.resolve_stackup(net, board=str(tmp_path / "pcb" / "c.kicad_pcb"), fd="off"))
    assert Path(out["path"]) == tmp_path / "netlist.stackup.json"
    assert Path(out["dru_path"]) == tmp_path / "pcb" / "c.kicad_dru"
    assert Path(out["path"]).is_file() and Path(out["dru_path"]).is_file()
    assert out["written"] is True and out["decisions_written"] == 2
    assert [c["name"] for c in out["stackup"]["copper"]] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert set(out["lines"]) == {"RF_IN", "RF_MID", "RF_OUT"}
    assert out["lines"]["RF_IN"]["primary_layer"] == "F.Cu"
    assert out["lines"]["RF_IN"]["status"] == "closed_form"
    assert out["dru"]["families"]["R6"] == 6
    again = _ok(pt.resolve_stackup(net, board=str(tmp_path / "pcb" / "c.kicad_pcb"), fd="off"))
    assert again["changed"] is False and again["decisions_written"] == 0


def test_resolve_stackup_dry_run_writes_nothing(tmp_path: Path) -> None:
    net = _netlist(tmp_path, load_example("cp2102n_uart"))
    out = _ok(pt.resolve_stackup(net, fd="off", write=False))
    assert out["written"] is False and out["path"] is None
    assert list(tmp_path.iterdir()) == [Path(net)]
    assert out["decision_drafts"] == 2 and out["lines"] == {}


def test_resolve_stackup_reports_design_errors(tmp_path: Path) -> None:
    fx = json.loads((ROOT / "tests/fixtures/stackups/st_e4.json").read_text(encoding="utf-8"))
    out = pt.resolve_stackup(_netlist(tmp_path, fx["design"]), fd="off")
    _refused(out, "ST-E4")
    assert out["errors"] and out["path"]                      # the artifact records them


@pytest.mark.parametrize("kwargs, words", [
    ({"netlist": ""}, ("netlist",)),
    ({"netlist": "nope/missing.json"}, ("not found",)),
    ({"netlist": "__FILE__", "fd": "sometimes"}, ("fd",)),
    ({"netlist": "__FILE__", "board": "x.json"}, ("kicad_pcb",)),
    ({"netlist": "__TEXT__"}, ("JSON",)),
])
def test_resolve_stackup_refusals(kwargs: dict, words: tuple, tmp_path: Path) -> None:
    good = _netlist(tmp_path, load_example("lm358_buffer"))
    text = tmp_path / "text.json"
    text.write_text("not json", encoding="utf-8")
    args = {k: (good if v == "__FILE__" else str(text) if v == "__TEXT__" else v)
            for k, v in kwargs.items()}
    _refused(pt.resolve_stackup(**args), *words)


# ---------------------------------------------------------------- impedance_calc
def test_impedance_calc_synthesises_on_the_default_stackup() -> None:
    out = _ok(pt.impedance_calc(target_ohm=50))
    assert out["analysis"] == "synthesis" and out["layer"] == "F.Cu"
    assert out["stackup"]["preset"] == "jlcpcb:JLC04161H-7628"
    res = out["result"]
    assert res["accuracy_class"] == "B" and res["line"] == "microstrip"
    assert abs(res["width"] - 0.348) <= 0.348 * 0.03                # D7.14 MS50_7628_mask
    assert abs(res["z_achieved"] - 50) <= 1.5
    assert res["ref"]["below"] == ["In1.Cu"]


def test_impedance_calc_checks_a_given_width_and_reads_presets_and_legacy() -> None:
    check = _ok(pt.impedance_calc(target_ohm=50, width_mm=0.35))
    assert check["analysis"] == "check" and check["result"]["width"] == 0.35
    assert 48 < check["result"]["z_achieved"] < 51
    thin = _ok(pt.impedance_calc(target_ohm=50, preset="jlcpcb:JLC04161H-3313"))
    assert thin["result"]["width"] < 0.175                              # masked, D7-07
    legacy = _ok(pt.impedance_calc(target_ohm=50, stackup_json='{"h": 0.2104, "er": 4.4}'))
    assert abs(legacy["result"]["width"] - 0.3726) <= 0.006            # H&J, unmasked
    assert legacy["result"]["accuracy_class"] == "A"
    diff = _ok(pt.impedance_calc(target_ohm=90, kind="diff", pair_gap_mm=0.15))
    assert diff["result"]["pair_gap"] == 0.15 and diff["kind"] == "diff"
    assert abs(diff["result"]["z_achieved"] - 90) <= 2.7
    cpwg = _ok(pt.impedance_calc(target_ohm=50, preset="generic:2L-1.6", line="cpwg",
                                 ground_gap_mm=0.2, freq_hz=9.15e8))
    assert cpwg["result"]["line"] == "cpwg" and cpwg["result"]["ground_gap"] == 0.2
    inner = _ok(pt.impedance_calc(target_ohm=50, preset="jlcpcb:JLC06161H-2116",
                                  layer="In2.Cu"))
    assert inner["result"]["line"] in ("stripline", "dual_stripline")


def test_impedance_calc_reports_a_design_net_as_the_resolver_does(tmp_path: Path) -> None:
    design = load_example("rf_lna_gainblock")
    net = _netlist(tmp_path, design)
    out = _ok(pt.impedance_calc(netlist=net, net="AMP_OUT"))
    rec = impedance.resolve_impedance(design, errors="record")["AMP_OUT"]
    assert out["net"] == "AMP_OUT" and out["layer"] == rec["primary_layer"]
    assert out["result"]["width"] == rec["layers"][rec["primary_layer"]]["width"]
    assert set(out["layers"]) == set(rec["layers"])
    synth = _ok(pt.impedance_calc(netlist=net, target_ohm=50, layer="B.Cu"))
    assert synth["net"] is None and synth["layer"] == "B.Cu"
    assert synth["stackup"]["source"] == "legacy"


@pytest.mark.parametrize("kwargs, words", [
    ({"target_ohm": 0}, ("target_ohm",)),
    ({"target_ohm": 50, "kind": "triple"}, ("kind",)),
    ({"target_ohm": 50, "width_mm": -1}, ("width_mm",)),
    ({"target_ohm": 50, "fd": "maybe"}, ("fd",)),
    ({"target_ohm": 50, "preset": "nofab:X"}, ("ST-E4",)),
    ({"target_ohm": 50, "preset": "jlcpcb:JLC04161H-7628", "stackup_json": "{}"}, ("one",)),
    ({"target_ohm": 50, "stackup_json": "[1, 2"}, ("stackup_json",)),
    ({"target_ohm": 50, "layer": "In1.Cu"}, ("ST-E9",)),
    ({"target_ohm": 50, "layer": "In7.Cu"}, ("In7.Cu",)),
    ({"net": "RF_IN"}, ("netlist",)),
])
def test_impedance_calc_refusals(kwargs: dict, words: tuple) -> None:
    _refused(pt.impedance_calc(**kwargs), *words)


def test_impedance_calc_unknown_net(tmp_path: Path) -> None:
    net = _netlist(tmp_path, load_example("rf_lna_gainblock"))
    _refused(pt.impedance_calc(netlist=net, net="V5"), "V5")
    odd = _netlist(tmp_path, {"nets": [{"x": 1}], "net_specs": ["RF"]}, "odd.json")
    _refused(pt.impedance_calc(netlist=odd, net="RF"), "RF")
    free = _ok(pt.impedance_calc(netlist=odd, target_ohm=50))       # malformed shapes tolerated
    assert free["result"]["width"] > 0


# ---------------------------------------------------------------- list_materials
def test_list_materials_lists_filters_and_details() -> None:
    every = _ok(pt.list_materials())
    ids = [m["id"] for m in every["materials"]]
    assert "rogers.ro4350b" in ids and "generic.fr4" in ids and ids == sorted(ids)
    assert every["count"] == len(ids) and len(every["data_version"]) == 64
    ro = next(m for m in every["materials"] if m["id"] == "rogers.ro4350b")
    assert ro["vendor"] == "Rogers" and any(d["value"] == 3.66 for d in ro["dk"])
    rf = _ok(pt.list_materials(category="rf-laminate"))
    assert rf["materials"] and all(m["category"] == "rf-laminate" for m in rf["materials"])
    one = _ok(pt.list_materials(material="rogers.ro4350b"))
    assert one["material"]["id"] == "rogers.ro4350b" and one["material"]["sources"]
    _refused(pt.list_materials(category="cheese"), "category")
    _refused(pt.list_materials(material="unobtainium"), "unobtainium")


# ---------------------------------------------------------------- list_stackup_presets
def test_list_stackup_presets_filters_and_truncates() -> None:
    jlc4 = _ok(pt.list_stackup_presets(fab="jlcpcb", copper_layers=4, limit=1000))
    names = [p["preset"] for p in jlc4["presets"]]
    assert {"jlcpcb:JLC04161H-7628", "jlcpcb:JLC04161H-3313"} <= set(names)
    assert all(p["copper_layers"] == 4 and p["fab"] == "jlcpcb" for p in jlc4["presets"])
    assert jlc4["fabs"][0]["id"] == "jlcpcb" and jlc4["fabs"][0]["soldermask"] is True
    few = _ok(pt.list_stackup_presets(limit=3))
    assert len(few["presets"]) == 3 and few["truncated"] is True and few["total"] > 3
    q = _ok(pt.list_stackup_presets(query="GENERIC:2l"))
    assert [p["preset"] for p in q["presets"]] == ["generic:2L-1.6"]
    assert q["presets"][0]["default_for"] == [1, 2]
    defaults = {p["preset"]: p["default_for"] for p in jlc4["presets"] if p["default_for"]}
    assert defaults == {"jlcpcb:JLC04161H-7628": [4]}
    for preset in ("jlcpcb:JLC04161H-3313", "oshpark:4L", "generic:2L-1.6"):
        rs, issues = stackup_model.resolve({"stackup": {"preset": preset}})
        assert rs is not None, [str(i) for i in issues]


@pytest.mark.parametrize("kwargs, word", [({"fab": "acme"}, "acme"),
                                          ({"copper_layers": 3}, "copper_layers"),
                                          ({"limit": 0}, "limit")])
def test_list_stackup_presets_refusals(kwargs: dict, word: str) -> None:
    _refused(pt.list_stackup_presets(**kwargs), word)


# ---------------------------------------------------------------- check_layer_count
def test_check_layer_count_strict_without_a_count(tmp_path: Path) -> None:
    out = _ok(pt.check_layer_count(_netlist(tmp_path, load_example("lm358_buffer"))))
    assert out["declared"] is False and out["napkin"] is False
    assert [i["id"] for i in out["issues"]] == ["ST-W12"]
    assert out["resolved"]["copper_layers"] == 4 and out["napkin_default"] is None


def test_check_layer_count_napkin_default_and_floor(tmp_path: Path) -> None:
    napkin = {**load_example("rf_coupon_50r"), "mode": "napkin"}
    napkin.pop("stackup")
    out = _ok(pt.check_layer_count(_netlist(tmp_path, napkin)))
    nd = out["napkin_default"]
    assert nd["copper_layers"] == 4 and nd["preset"] == "jlcpcb:JLC04161H-3313"
    assert nd["decision_draft"]["stage"] == "requirements"
    assert not decisions.validate_decision({**nd["decision_draft"], "id": "dec-000000000000",
                                            "ts": "2026-10-10T00:00:00+00:00"})
    assert [f["id"] for f in out["floors"]] == ["F1"]
    two = {**load_example("rf_coupon_50r"), "copper_layers": 2,
           "stackup": {"preset": "generic:2L-1.6", "fab": "jlcpcb", "rationale": "cheap"}}
    low = _ok(pt.check_layer_count(_netlist(tmp_path, two, "two.json")))
    assert [i["id"] for i in low["issues"]] == ["ST-W3"] and low["declared"] is True
    assert "floor F1" in low["issues"][0]["message"]
    assert low["resolved"]["copper_layers"] == 2 and low["resolved"]["fab"] == "jlcpcb"


def test_check_layer_count_refusals(tmp_path: Path) -> None:
    _refused(pt.check_layer_count(""), "netlist")
    bad = {**load_example("lm358_buffer"), "copper_layers": 3}
    out = pt.check_layer_count(_netlist(tmp_path, bad))
    _refused(out, "ST-E2")
    assert out["issues"][0]["id"] == "ST-E2"


def test_every_tool_survives_a_non_object_netlist(tmp_path: Path) -> None:
    arr = tmp_path / "arr.json"
    arr.write_text("[1, 2]", encoding="utf-8")
    _refused(pt.resolve_stackup(str(arr)), "object")
    _refused(pt.check_layer_count(str(arr)), "object")
    _refused(pt.impedance_calc(netlist=str(arr)), "object")


def test_resolve_stackup_on_a_scratch_copy_of_cp2102n_uart(tmp_path: Path) -> None:
    work = tmp_path / "cp2102n_uart"
    shutil.copytree(ROOT / "examples" / "cp2102n_uart", work)
    out = _ok(pt.resolve_stackup(str(work / "netlist.json"), fd="off"))
    assert Path(out["path"]) == work / "netlist.stackup.json"
    assert Path(out["dru_path"]) == work / "netlist.kicad_dru"
    assert pr.is_fresh(out["path"], json.loads((work / "netlist.json").read_text()),
                       board_path=work / "netlist.kicad_pcb")
