"""Harness helpers: rp_route_hash.route_hash (stage-cache key), the command
line (rp_cli), rp_stage_fab.sourcing (G5), the board-cli plumbing behind
rp_kicad.drc / render, and the run_pipeline aliases its callers rely on.

These test the CORE harness, so no pipeline feature module takes part
(core_only); test_the_discovered_features_keep_the_registry_consistent is the
one test that runs with every discovered pipeline/rp_feature_*.py."""
from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

import pytest

import kicad_server
import rp_cli
import rp_context
import rp_hooks
import rp_kicad
import rp_paths
import rp_route
import rp_route_hash
import rp_stage_fab
import rp_stage_place
import run_pipeline as rp
import toolchain as tc

HEX64 = re.compile(r"^[0-9a-f]{64}$")
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture(autouse=True)
def core_only():
    """No feature module takes part; the previous selection comes back after."""
    previous = rp_hooks.selection()
    rp_hooks.set_features([])
    yield
    rp_hooks.set_features(previous)


# --- the module split: rp_paths and the run_pipeline aliases ---------------------

def test_rp_paths_puts_the_server_on_sys_path_once():
    plugin = Path(rp.__file__).resolve().parents[1]
    assert rp_paths.SERVER == plugin / "server"
    entries = (str(rp_paths.SERVER), str(rp_paths.SERVER / "tools"))
    before = [sys.path.count(e) for e in entries]
    assert all(n >= 1 for n in before)
    rp_paths.ensure_on_path()                        # a second bootstrap adds nothing
    assert [sys.path.count(e) for e in entries] == before


def test_rp_paths_bootstrap_puts_tools_ahead_of_server(monkeypatch):
    """A bare `python pipeline/run_pipeline.py` has neither entry yet: both go
    to the front, server/tools first (the order run_pipeline always used)."""
    entries = {str(rp_paths.SERVER), str(rp_paths.SERVER / "tools")}
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p not in entries])
    rp_paths.ensure_on_path()
    assert sys.path[:2] == [str(rp_paths.SERVER / "tools"), str(rp_paths.SERVER)]


# every name pipeline/run_all.py and tests/test_toolchain.py use on run_pipeline
# (they stay unedited) -> the object it must still be
ALIASES = {
    "k": kicad_server, "tc": tc, "SERVER": rp_paths.SERVER,
    "_board_cli": rp_kicad.board_cli, "_drc": rp_kicad.drc, "_board_fix": rp_kicad.board_fix,
    "RunContext": rp_context.RunContext, "PACK_SCALES": rp_stage_place.PACK_SCALES,
    "AUTOROUTE_MAX_PASSES": rp_route.AUTOROUTE_MAX_PASSES,
    "AUTOROUTE_TIMEOUT_S": rp_route.AUTOROUTE_TIMEOUT_S,
    "_route_hash": rp_route_hash.route_hash, "_sourcing": rp_stage_fab.sourcing,
    "parse_cli": rp_cli.parse_cli, "board_dims": rp_cli.board_dims,
    "UsageError": rp_cli.UsageError, "CliArgs": rp_cli.CliArgs,
    "AUTO_BOARD_DIMS": rp_cli.AUTO_BOARD_DIMS, "USAGE": rp_cli.USAGE,
}


@pytest.mark.parametrize("name", sorted(ALIASES))
def test_run_pipeline_keeps_the_aliases_its_callers_use(name):
    assert getattr(rp, name) is ALIASES[name]


def test_run_all_imports_against_the_split_harness():
    import run_all
    assert run_all.rp is rp and callable(rp.run) and callable(rp.main)


def _hash_after(design: dict, mutate) -> str:
    d = copy.deepcopy(design)
    mutate(d)
    return rp_route_hash.route_hash(d)


# --- _route_hash ---------------------------------------------------------------

def test_route_hash_is_sha256_hex_and_deterministic(hat_design):
    h = rp_route_hash.route_hash(hat_design)
    assert HEX64.match(h)
    assert rp_route_hash.route_hash(copy.deepcopy(hat_design)) == h


def test_route_hash_does_not_mutate_design(hat_design):
    snapshot = copy.deepcopy(hat_design)
    rp_route_hash.route_hash(hat_design)
    assert hat_design == snapshot


@pytest.mark.parametrize("mutate", [
    lambda d: d["components"][0].__setitem__("value", "CHANGED"),
    lambda d: d["components"][3].__setitem__("lcsc", "C12345"),
    lambda d: d["components"][3].__setitem__("mpn", "ABC-123"),
    lambda d: d["components"][3].__setitem__("thermal", {"p_diss": 9.0}),
    lambda d: d["components"][3].__setitem__("rds_mohm", 5),
    lambda d: d.__setitem__("thermal", {"t_ambient": 85}),
    lambda d: d.__setitem__("title", "renamed"),
    lambda d: d.__setitem__("require_sourcing", True),
    lambda d: d.__setitem__("height_zones", []),
], ids=["value", "lcsc", "mpn", "part-thermal", "rds", "design-thermal", "title",
        "require_sourcing", "height_zones"])
def test_route_hash_stable_under_non_geometric_edits(hat_design, mutate):
    assert _hash_after(hat_design, mutate) == rp_route_hash.route_hash(hat_design)


@pytest.mark.parametrize("mutate", [
    lambda d: d["components"][0].__setitem__("x", d["components"][0]["x"] + 0.1),
    lambda d: d["components"][0].__setitem__("y", 0.0),
    lambda d: d["components"][0].__setitem__("rot", 0),
    lambda d: d["components"][1].pop("side"),
    lambda d: d["components"][0].__setitem__("footprint", "Other:FP"),
    lambda d: d["components"][3].__setitem__("interior", True),
    lambda d: d["nets"]["GND"].append(["TP1", "1"]),
    lambda d: d["nets"].pop("CC1"),
    lambda d: d["net_specs"]["5V_PI"].__setitem__("current", 5.0),
    lambda d: d["preroutes"][0].__setitem__("width", 2.0),
    lambda d: d["preroutes"].pop(),
    lambda d: d["board"].__setitem__("w", 70.0),
    lambda d: d["mounting_holes"]["positions"].pop(),
    lambda d: d.__setitem__("class", 3),
    lambda d: d.__setitem__("emc", {"max_freq_hz": 1e8}),
    lambda d: d.__setitem__("antenna_keepout", {"x": 0, "y": 0, "w": 5, "h": 5}),
    lambda d: d["components"].pop(),
], ids=["x", "y", "rot", "side", "footprint", "interior", "net-add-pad", "net-drop",
        "net_spec-current", "preroute-width", "preroute-drop", "board", "holes",
        "class", "emc", "antenna_keepout", "component-drop"])
def test_route_hash_changes_under_geometric_edits(hat_design, mutate):
    assert _hash_after(hat_design, mutate) != rp_route_hash.route_hash(hat_design)


def test_route_hash_ignores_component_order(hat_design):
    shuffled = copy.deepcopy(hat_design)
    shuffled["components"].reverse()
    assert rp_route_hash.route_hash(shuffled) == rp_route_hash.route_hash(hat_design)


def test_route_hash_ignores_net_key_order(buck_design):
    reordered = copy.deepcopy(buck_design)
    reordered["nets"] = dict(reversed(list(buck_design["nets"].items())))
    assert rp_route_hash.route_hash(reordered) == rp_route_hash.route_hash(buck_design)


def test_route_hash_differs_between_designs(buck_design, hat_design):
    assert rp_route_hash.route_hash(buck_design) != rp_route_hash.route_hash(hat_design)


def test_route_hash_of_empty_design_is_well_defined():
    assert HEX64.match(rp_route_hash.route_hash({}))
    assert rp_route_hash.route_hash({}) == rp_route_hash.route_hash({"components": []})


# --- the route-hash registry ------------------------------------------------------

def test_route_hash_registry_lists_the_keys_that_move_copper():
    reg = rp_route_hash.registry()
    design_keys = set(reg.design_keys)
    assert {"planes", "stackup", "thermal.ep_vias_min", "copper_layers"} <= design_keys
    assert {"edge_row", "overhang", "at"} <= set(reg.component_keys)
    # deliberately excluded: they never move copper (the G3/G4 gates re-run on a
    # cached board anyway), so they must never appear in the registry
    assert "height_zones" not in design_keys and "thermal" not in design_keys
    assert not set(reg.excluded) & design_keys
    assert {"height_zones", "thermal"} <= set(reg.excluded)


def test_the_discovered_features_keep_the_registry_consistent():
    """Every pipeline/rp_feature_*.py together: the merged registry keeps the
    core keys, hashes no excluded key, and every example still hashes."""
    rp_hooks.set_features(None)
    reg = rp_route_hash.registry()
    assert set(rp_route_hash.ROUTE_HASH_DESIGN_KEYS) <= set(reg.design_keys)
    assert set(rp_route_hash.ROUTE_HASH_COMPONENT_KEYS) <= set(reg.component_keys)
    assert set(rp_route_hash.ROUTE_HASH_EXCLUDED) <= set(reg.excluded)
    assert not set(reg.excluded) & set(reg.design_keys)
    for netlist in sorted(EXAMPLES.glob("*/netlist.json")):
        design = json.loads(netlist.read_text(encoding="utf-8"))
        assert HEX64.match(rp_route_hash.route_hash(design, netlist.parent)), netlist


@pytest.mark.parametrize("mutate", [
    lambda d: d.__setitem__("planes", {"In2.Cu": "VBAT"}),
    lambda d: d.__setitem__("stackup", {"h": 0.2, "er": 4.2}),
    lambda d: d["components"][3].__setitem__("edge_row", True),
    lambda d: d["components"][3].__setitem__("overhang", 7.0),
    lambda d: d.__setitem__("thermal", {"ep_vias_min": 6}),
    lambda d: d["components"][3].__setitem__("at", [10.0, 12.0, 90]),
    lambda d: d.__setitem__("copper_layers", 2),
], ids=["planes", "stackup", "edge_row", "overhang", "thermal.ep_vias_min", "at",
        "copper_layers"])
def test_route_hash_changes_when_a_registry_key_changes(hat_design, mutate):
    assert _hash_after(hat_design, mutate) != rp_route_hash.route_hash(hat_design)


def test_route_hash_sees_only_ep_vias_min_of_the_thermal_block(hat_design):
    base = copy.deepcopy(hat_design)
    base["thermal"] = {"t_ambient": 25, "tj_margin": 0.8, "ep_vias_min": 6}
    h = rp_route_hash.route_hash(base)
    assert _hash_after(base, lambda d: d["thermal"].__setitem__("t_ambient", 85)) == h
    assert _hash_after(base, lambda d: d["thermal"].__setitem__("tj_margin", 0.7)) == h
    assert _hash_after(base, lambda d: d["thermal"].__setitem__("ep_vias_min", 9)) != h
    assert _hash_after(base, lambda d: d.pop("thermal")) != h


@pytest.mark.parametrize("key", rp_route_hash.ROUTE_HASH_EXCLUDED)
def test_route_hash_ignores_every_excluded_key(hat_design, key):
    assert key in rp_route_hash.registry().excluded
    assert _hash_after(hat_design, lambda d: d.__setitem__(key, {"changed": key})) == \
        rp_route_hash.route_hash(hat_design)


@pytest.mark.parametrize("key", rp_route_hash.ROUTE_HASH_DESIGN_KEYS)
def test_route_hash_moves_with_every_registry_design_key(hat_design, key):
    assert key in rp_route_hash.registry().design_keys
    top = key.split(".")[0]
    changed = (lambda d: d.__setitem__(top, {key.split(".")[-1]: "changed"})) if "." in key \
        else (lambda d: d.__setitem__(key, {"changed": key}))
    assert _hash_after(hat_design, changed) != rp_route_hash.route_hash(hat_design)


@pytest.mark.parametrize("thermal", [None, "junk", 3, []])
def test_route_hash_tolerates_a_malformed_thermal_block(thermal):
    assert rp_route_hash.route_hash({"thermal": thermal}) == rp_route_hash.route_hash({})


def test_route_hash_moves_with_the_new_keys_on_real_examples(load_design):
    node = load_design("esp32c3_env_node")
    i = next(i for i, c in enumerate(node["components"]) if c["ref"] == "U1")
    assert node["components"][i]["edge_row"] is True and node["components"][i]["overhang"] == 7.0
    assert _hash_after(node, lambda d: d["components"][i].__setitem__("overhang", 5.0)) != \
        rp_route_hash.route_hash(node)
    assert _hash_after(node, lambda d: d["components"][i].pop("edge_row")) != \
        rp_route_hash.route_hash(node)
    coupon = load_design("rf_coupon_50r")
    assert _hash_after(coupon, lambda d: d["stackup"].__setitem__("er", 3.5)) != \
        rp_route_hash.route_hash(coupon)


# --- command line ------------------------------------------------------------------

@pytest.mark.parametrize("argv, expected", [
    (["n.json"], rp_cli.CliArgs("n.json", None, None, "")),
    (["n.json", "30", "40"], rp_cli.CliArgs("n.json", 30.0, 40.0, "")),
    (["n.json", "30", "40.5", "p.json"], rp_cli.CliArgs("n.json", 30.0, 40.5, "p.json")),
    (["n.json", "30", "40", "-"], rp_cli.CliArgs("n.json", 30.0, 40.0, "")),
    (["n.json", "30", "40", ""], rp_cli.CliArgs("n.json", 30.0, 40.0, "")),
    (["n.json", "p.json"], rp_cli.CliArgs("n.json", None, None, "p.json")),
    (["n.json", "-"], rp_cli.CliArgs("n.json", None, None, "")),
])
def test_parse_cli_accepts(argv, expected):
    assert rp_cli.parse_cli(argv) == expected


@pytest.mark.parametrize("argv, message", [
    ([], "missing <netlist.json>"),
    (["n.json", "30"], "give both <board_w> and <board_h>, or neither"),
    (["n.json", "30", "p.json"], "give both <board_w> and <board_h>, or neither"),
    (["n.json", "0", "40"], "board_w must be a positive number of mm, got '0'"),
    (["n.json", "30", "-5"], "board_h must be a positive number of mm, got '-5'"),
    (["n.json", "nan", "40"], "board_w must be a positive number of mm, got 'nan'"),
    (["n.json", "30", "40", "p.json", "extra"], "unexpected argument(s): extra"),
    (["n.json", "p.json", "extra"], "unexpected argument(s): extra"),
])
def test_parse_cli_rejects(argv, message):
    with pytest.raises(rp_cli.UsageError) as err:
        rp_cli.parse_cli(argv)
    assert str(err.value) == message


AUTO = {"components": [{"ref": "R1", "footprint": "R:R"}, {"ref": "R2", "footprint": "R:R"}]}
FIXED = {"components": [{"ref": "R1", "x": 1, "y": 2}, {"ref": "R2", "at": [5, 5, 0]}]}


def test_board_dims_prefers_the_command_line():
    assert rp_cli.board_dims({**FIXED, "board": {"w": 9, "h": 9}}, 30.0, 40.0) == \
        (30.0, 40.0, "command line")


def test_board_dims_falls_back_to_the_board_block():
    assert rp_cli.board_dims({**AUTO, "board": {"w": 65, "h": 56.5}}, None, None) == \
        (65.0, 56.5, "netlist board block")


def test_board_dims_auto_sized_board_gets_the_builder_default():
    w, h, source = rp_cli.board_dims(AUTO, None, None)
    assert (w, h) == rp_cli.AUTO_BOARD_DIMS == (50.0, 25.0)
    assert "auto" in source


def test_board_dims_full_placement_without_a_size_is_a_clear_error():
    with pytest.raises(rp_cli.UsageError, match=r'every component has a fixed position.*"board"'):
        rp_cli.board_dims(FIXED, None, None)


def test_board_dims_counts_placement_file_refs_as_fixed():
    assert rp_cli.board_dims(AUTO, None, None, frozenset({"R1"}))[:2] == rp_cli.AUTO_BOARD_DIMS
    with pytest.raises(rp_cli.UsageError, match="fixed position"):
        rp_cli.board_dims(AUTO, None, None, frozenset({"R1", "R2"}))


def test_board_dims_outline_only_board_block_is_left_to_the_builder():
    dims = rp_cli.board_dims({**FIXED, "board": {"outline": {"loop": [[0, 0], [9, 0], [9, 9]]}}},
                         None, None)
    assert dims[:2] == rp_cli.AUTO_BOARD_DIMS


@pytest.mark.parametrize("board", [{"w": "wide", "h": 10}, {"w": 10}, {"w": -1, "h": 5}])
def test_board_dims_rejects_a_malformed_board_block(board):
    with pytest.raises(rp_cli.UsageError, match="board block"):
        rp_cli.board_dims({**AUTO, "board": board}, None, None)


@pytest.fixture
def fake_run(monkeypatch):
    calls = []

    def run(netlist, w, h, placement=""):
        calls.append((netlist, w, h, placement))
        return {"PASS": len(calls) == 1, "gates": [], "gate_summary": "[PASS] G1"}

    monkeypatch.setattr(rp, "run", run)
    return calls


def _netlist(tmp_path, design: dict) -> str:
    path = tmp_path / "netlist.json"
    path.write_text(json.dumps(design), encoding="utf-8")
    return str(path)


def test_main_without_dims_uses_the_board_block(tmp_path, fake_run, capsys):
    netlist = _netlist(tmp_path, {**FIXED, "board": {"w": 65, "h": 56.5}})
    assert rp.main([netlist]) == 0
    assert fake_run == [(netlist, 65.0, 56.5, "")]
    out = capsys.readouterr()
    assert json.loads(out.out)["PASS"] is True
    assert "board 65.0 x 56.5 mm (netlist board block)" in out.err
    assert out.err.rstrip().endswith("[PASS] G1")


def test_main_without_dims_on_an_auto_board_uses_the_default(tmp_path, fake_run):
    netlist = _netlist(tmp_path, AUTO)
    assert rp.main([netlist]) == 0
    assert fake_run == [(netlist, 50.0, 25.0, "")]


def test_main_explicit_dims_and_placement_pass_through(tmp_path, fake_run):
    assert rp.main([str(tmp_path / "unread.json"), "30", "30", "place.json"]) == 0
    assert fake_run == [(str(tmp_path / "unread.json"), 30.0, 30.0, "place.json")]


def test_main_placement_file_that_fixes_every_part_needs_a_size(tmp_path, fake_run, capsys):
    netlist = _netlist(tmp_path, AUTO)
    place = tmp_path / "place.json"
    place.write_text(json.dumps({"R1": [1, 1, 0], "R2": [5, 1, 0]}), encoding="utf-8")
    assert rp.main([netlist, str(place)]) == 2
    assert fake_run == []
    assert "every component has a fixed position" in capsys.readouterr().err


def test_main_full_placement_without_size_is_an_error_not_an_indexerror(tmp_path, fake_run,
                                                                       capsys):
    netlist = _netlist(tmp_path, FIXED)
    assert rp.main([netlist]) == 2
    err = capsys.readouterr().err
    assert err.startswith("run_pipeline: error: every component has a fixed position")
    assert rp_cli.USAGE in err and fake_run == []


@pytest.mark.parametrize("argv, message", [
    ([], "missing <netlist.json>"),
    (["n.json", "30"], "give both <board_w> and <board_h>"),
])
def test_main_usage_errors_exit_2(argv, message, fake_run, capsys):
    assert rp.main(argv) == 2
    assert message in capsys.readouterr().err and fake_run == []


def test_main_unreadable_netlist_is_a_usage_error(tmp_path, fake_run, capsys):
    assert rp.main([str(tmp_path / "missing.json")]) == 2
    assert "cannot read the netlist" in capsys.readouterr().err


def test_main_unreadable_placement_file_is_a_usage_error(tmp_path, fake_run, capsys):
    netlist = _netlist(tmp_path, AUTO)
    (tmp_path / "place.json").write_text("[1, 2]", encoding="utf-8")
    assert rp.main([netlist, str(tmp_path / "place.json")]) == 2
    assert "placement file" in capsys.readouterr().err


def test_main_help_exits_0(fake_run, capsys):
    assert rp.main(["--help"]) == 0
    assert rp_cli.USAGE in capsys.readouterr().out and fake_run == []


def test_main_exit_code_follows_the_verdict(tmp_path, fake_run):
    netlist = _netlist(tmp_path, AUTO)
    assert rp.main([netlist]) == 0          # the fake passes its first run
    assert rp.main([netlist]) == 1          # and fails every later one


# --- _sourcing -------------------------------------------------------------------

def test_sourcing_examples_carry_no_part_numbers(hat_design):
    rep = rp_stage_fab.sourcing(hat_design)
    tps = {c["ref"] for c in hat_design["components"] if c["ref"].startswith("TP")}
    assert len(tps) == 5
    assert set(rep["missing"]) == {c["ref"] for c in hat_design["components"]} - tps
    assert rep["bad"] == [] and rep["required"] is False


def test_sourcing_lcsc_or_mpn_satisfies():
    d = {"components": [{"ref": "R1", "lcsc": "C12345"},
                        {"ref": "R2", "mpn": "RC0603FR-0710KL"},
                        {"ref": "R3", "lcsc": "C1234567", "mpn": "x"},
                        {"ref": "R4"}]}
    rep = rp_stage_fab.sourcing(d)
    assert rep == {"missing": ["R4"], "bad": [], "required": False}


@pytest.mark.parametrize("lcsc", ["12345", "c12345", "C12", "C-12345", "LCSC C12345", "C12345 "])
def test_sourcing_flags_malformed_lcsc(lcsc):
    rep = rp_stage_fab.sourcing({"components": [{"ref": "U1", "lcsc": lcsc}]})
    assert rep["missing"] == []
    assert rep["bad"] == [f"U1: lcsc '{lcsc}' is not Cxxxxx format"]


@pytest.mark.parametrize("lcsc", ["C123", "C12345", "C1234567890"])
def test_sourcing_accepts_well_formed_lcsc(lcsc):
    assert rp_stage_fab.sourcing({"components": [{"ref": "U1", "lcsc": lcsc}]})["bad"] == []


def test_sourcing_non_string_and_null_values():
    d = {"components": [{"ref": "U1", "lcsc": 12345}, {"ref": "U2", "lcsc": None, "mpn": None},
                        {"ref": "U3", "lcsc": "", "mpn": ""}]}
    rep = rp_stage_fab.sourcing(d)
    assert rep["bad"] == ["U1: lcsc '12345' is not Cxxxxx format"]
    assert rep["missing"] == ["U2", "U3"]


def test_sourcing_exempts_testpoints_holes_fiducials():
    d = {"components": [{"ref": "TP1"}, {"ref": "H3"}, {"ref": "FID2"},
                        {"ref": "TP2", "lcsc": "bad"}, {"ref": "R1"}]}
    rep = rp_stage_fab.sourcing(d)
    assert rep["missing"] == ["R1"] and rep["bad"] == []


def test_sourcing_required_flag_from_design():
    assert rp_stage_fab.sourcing({"components": [], "require_sourcing": True})["required"] is True
    assert rp_stage_fab.sourcing({"components": [], "require_sourcing": 0})["required"] is False
    assert rp_stage_fab.sourcing({})["required"] is False


# --- _drc / _render use the BOARD kicad-cli ------------------------------------

def test_drc_runs_the_board_cli_and_records_it(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, **kw):
        calls.append(list(args))
        rpt = args[args.index("-o") + 1]
        (tmp_path / rpt).write_text(json.dumps(
            {"violations": [{"type": "clearance"}], "unconnected_items": [{}]}), encoding="utf-8")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(tc, "board_cli", lambda pcb="": "S:/9.0/kicad-cli.exe")
    monkeypatch.setattr(tc, "kicad_cli", lambda: "N:/10.99/kicad-cli.exe")
    monkeypatch.setattr(rp_kicad.subprocess, "run", fake_run)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")   # drc_guard checks it
    res = rp_kicad.drc("b.kicad_pcb")
    assert calls[0][:3] == ["S:/9.0/kicad-cli.exe", "pcb", "drc"]
    assert res == {"errors": 1, "unconnected": 1, "types": {"clearance": 1},
                   "kicad_cli": "S:/9.0/kicad-cli.exe"}


def test_render_runs_the_board_cli(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, **kw):
        calls.append(list(args))
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "no"})()

    monkeypatch.setattr(tc, "board_cli", lambda pcb="": "S:/9.0/kicad-cli.exe")
    monkeypatch.setattr(rp_kicad.subprocess, "run", fake_run)
    assert rp_kicad.render(str(tmp_path / "b.kicad_pcb"), "final") == []
    assert [c[0] for c in calls] == ["S:/9.0/kicad-cli.exe"] * 2
    assert [c[c.index("--side") + 1] for c in calls] == ["top", "bottom"]


def test_drc_and_render_are_loud_without_a_kicad_cli(monkeypatch):
    monkeypatch.setattr(tc, "board_cli", lambda pcb="": None)
    with pytest.raises(RuntimeError, match="kicad-cli not found"):
        rp_kicad.drc("b.kicad_pcb")
    with pytest.raises(RuntimeError, match="kicad-cli not found"):
        rp_kicad.render("b.kicad_pcb", "place")
