"""physics_resolve (D7-13a, ADR D7.4 2a sections): the physical-design artifact
<netlist stem>.stackup.json, the managed <board stem>.kicad_dru, decision drafts, freshness,
the FD cache and the CLI."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import decisions
import dru_writer
import dsn_layers
import fieldsolve2d
import impedance
import materials as mat
import physics_resolve as pr
import stackup_model
import swig_stackup
from conftest import load_example

ROOT = Path(__file__).resolve().parents[1]
STACKUPS = ROOT / "tests" / "fixtures" / "stackups"
SCRIPT = ROOT / "server" / "tools" / "physics_resolve.py"
FIXTURES = sorted(p.stem for p in STACKUPS.glob("*.json"))
SECTIONS = ("stackup", "lines", "dsn", "dru", "decision_drafts", "warnings", "errors")
HEX64 = 64


def _fixture(name: str) -> dict:
    return json.loads((STACKUPS / f"{name}.json").read_text(encoding="utf-8"))


def _netlist(tmp_path: Path, design: dict, name: str = "netlist.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(design, indent=1), encoding="utf-8")
    return path


def _usb_design() -> dict:
    """The default 4L JLC 7628 board with a 90 ohm USB pair declared on one net only."""
    return {"name": "usb", "nets": {"GND": [], "+3V3": [], "USB_DP": [], "USB_DM": []},
            "net_specs": {"USB_DP": {"diff": 90, "gap": 0.15, "diff_pair": "USB_DM",
                                     "layers": ["F.Cu"]}}}


def _masked_50r() -> dict:
    """A class B line (JLC mask, Wan-Hoorfar) on the default 4L stackup, F.Cu only."""
    return {"name": "m50", "nets": {"GND": [], "+3V3": [], "RF": []},
            "net_specs": {"RF": {"impedance": 50, "layers": ["F.Cu"]}}}


def _walk_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _walk_keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _walk_keys(v)}
    return set()


# ---------------------------------------------------------------- shape
def test_section_builders_are_the_2a_sections_in_order() -> None:
    assert tuple(name for name, _ in pr.SECTION_BUILDERS) == SECTIONS
    assert pr.COLLECTORS == ("decision_drafts", "warnings", "errors")
    assert all(callable(fn) for _, fn in pr.SECTION_BUILDERS)


@pytest.mark.parametrize("name", FIXTURES)
def test_artifact_for_every_stackup_fixture(name: str, tmp_path: Path) -> None:
    fx = _fixture(name)
    design, expect = fx["design"], fx["expect"]
    net = _netlist(tmp_path, design)
    art = pr.resolve_design(design, net, board_path=tmp_path / "netlist.kicad_pcb", fd="off")
    assert list(art) == ["schema", "design_digest", "data_version", *SECTIONS]
    assert art["schema"] == pr.SCHEMA == "kicad-eda/physical@1"
    assert len(art["design_digest"]) == HEX64 and len(art["data_version"]) == HEX64
    assert json.loads(pr.artifact_text(art)) == art            # plain JSON
    if "error" in expect and expect["error"] != "ST-E12":
        assert art["stackup"] is None and art["dsn"] is None and art["dru"] is None
        assert art["lines"] == {}
        assert any(e.startswith(expect["error"]) for e in art["errors"])
        return
    rs, _ = stackup_model.resolve(design)
    assert art["stackup"] == rs.to_dict()
    assert swig_stackup.stackup_from_dict(art["stackup"]).to_dict() == rs.to_dict()
    assert art["dsn"]["signal_layers"] == list(rs.routable_layers())
    assert art["dsn"]["unused_layers"] == [c.name for c in rs.copper if c.role == "unused"]
    planes = {c.net for c in rs.copper if c.role == "plane"}
    assert set(art["dsn"]["plane_nets"]) == planes
    dsn_layers.load_policy(art["dsn"])                             # the D7-19 contract
    assert art["dru"]["path"] == "netlist.kicad_dru"
    assert art["dru"]["rules"] == 1 + sum(art["dru"]["families"].values())
    assert len(art["decision_drafts"]) >= expect.get("drafts", 0)
    if expect.get("error") == "ST-E12":
        assert any(e.startswith("ST-E12") for e in art["errors"])


def test_artifact_holds_no_run_times_ids_or_timestamps(tmp_path: Path) -> None:
    net = _netlist(tmp_path, _usb_design())
    art = pr.resolve_design(_usb_design(), net, fd="off")
    assert not {"seconds", "cells", "ts", "id", "spent_s", "solves"} & _walk_keys(art)
    for draft in art["decision_drafts"]:
        assert not decisions.validate_decision({**draft, "id": "dec-000000000000",
                                                "ts": "2026-10-10T00:00:00+00:00"})


def test_legacy_rf_examples_resolve_their_lines_dsn_and_dru(tmp_path: Path) -> None:
    for name in ("rf_coupon_50r", "rf_lna_gainblock"):
        design = load_example(name)
        net = _netlist(tmp_path, design, f"{name}.json")
        art = pr.resolve_design(design, net, fd="off")
        imp = [n for n, s in design["net_specs"].items() if "impedance" in s]
        assert list(art["lines"]) == imp and not art["errors"]
        for rec in art["lines"].values():
            assert rec["primary_layer"] == "F.Cu" and set(rec["layers"]) == {"F.Cu", "B.Cu"}
            assert rec["accuracy_class"] == "A" and rec["status"] == "closed_form"
            assert abs(rec["track_width"] - 0.365) < 0.011                 # D7-07 legacy width
            assert "decision_drafts" not in rec
        assert art["stackup"]["source"] == "legacy"
        nl = art["dsn"]["net_layers"]
        assert set(nl) == set(imp)
        assert nl["RF_IN"]["primary"] == "F.Cu"
        assert nl["RF_IN"]["widths"] == {"F.Cu": art["lines"]["RF_IN"]["layers"]["F.Cu"]["width"],
                                         "B.Cu": art["lines"]["RF_IN"]["layers"]["B.Cu"]["width"]}
        assert nl["RF_IN"]["window"] == art["lines"]["RF_IN"]["width_window"]
        fam = art["dru"]["families"]
        assert fam == {"R8": 2, "R7": 0, "R6": 2 * len(imp)}
        assert any(w.startswith("ST-W7") for w in art["warnings"])
    coupon = pr.resolve_design(load_example("rf_coupon_50r"), tmp_path / "c.json", fd="off")
    assert coupon["dsn"]["plane_nets"] == {"GND": ["In1.Cu", "In2.Cu"]}   # no rail: GND twice
    assert [d["override"] for d in coupon["decision_drafts"]] == ["planes.In1.Cu",
                                                                  "planes.In2.Cu"]


def test_diff_pair_partner_gets_the_dsn_layers_and_dru_rules(tmp_path: Path) -> None:
    art = pr.resolve_design(_usb_design(), _netlist(tmp_path, _usb_design()), fd="off")
    assert list(art["lines"]) == ["USB_DP"]
    nl = art["dsn"]["net_layers"]
    assert list(nl) == ["USB_DM", "USB_DP"] and nl["USB_DM"] == nl["USB_DP"]
    assert nl["USB_DP"]["widths"] == {"F.Cu": art["lines"]["USB_DP"]["layers"]["F.Cu"]["width"]}
    rules = pr.dru_rules(_usb_design(), art["stackup"], art["lines"])
    names = [r.name for r in rules]
    assert "kicad_eda_width_USB_DM_F.Cu" in names and "kicad_eda_offlayer_USB_DM_B.Cu" in names
    assert art["dru"]["families"] == {"R8": 2, "R7": 2, "R6": 2}


def test_single_sided_dsn_has_only_f_cu(tmp_path: Path) -> None:
    design = _fixture("single_sided")["design"]
    art = pr.resolve_design(design, tmp_path / "netlist.json", fd="off")
    assert art["dsn"] == {"signal_layers": ["F.Cu"], "plane_nets": {}, "unused_layers": ["B.Cu"],
                          "net_layers": {}}


def test_resolve_never_mutates_the_design(tmp_path: Path) -> None:
    design = _usb_design()
    before = copy.deepcopy(design)
    pr.resolve_design(design, tmp_path / "netlist.json", fd="off")
    pr.write_artifact(_netlist(tmp_path, design), design, fd="off")
    assert design == before


# ---------------------------------------------------------------- collectors
def test_collectors_gather_a_section_appended_later(tmp_path: Path, monkeypatch) -> None:
    draft = {"stage": "G1", "question": "q?", "decision": "d", "rationale": "r",
             "alternatives": ["a"], "confidence": "med", "source": "test", "reversible": True,
             "override": None}

    def rf(ctx: pr.Context) -> pr.Built:
        assert ctx.section("lines") is not None
        return pr.Built({"chains": []}, drafts=(draft,), warnings=("rf: w",), errors=("rf: e",))

    monkeypatch.setattr(pr, "SECTION_BUILDERS", (*pr.SECTION_BUILDERS, ("rf", rf)))
    art = pr.resolve_design(_masked_50r(), tmp_path / "netlist.json", fd="off")
    assert list(art)[-1] == "rf" and art["rf"] == {"chains": []}
    assert draft in art["decision_drafts"]
    assert "rf: w" in art["warnings"] and "rf: e" in art["errors"]


def test_an_invalid_draft_is_an_error_not_a_decision(tmp_path: Path, monkeypatch) -> None:
    def bad(ctx: pr.Context) -> pr.Built:
        return pr.Built(None, drafts=({"stage": "nowhere"},))

    monkeypatch.setattr(pr, "SECTION_BUILDERS", (*pr.SECTION_BUILDERS, ("bad", bad)))
    art = pr.resolve_design(_masked_50r(), tmp_path / "netlist.json", fd="off")
    assert {"stage": "nowhere"} not in art["decision_drafts"]
    assert any("decision draft" in e for e in art["errors"])


# ---------------------------------------------------------------- write_artifact
def test_write_artifact_writes_the_artifact_and_the_board_stem_dru(tmp_path: Path) -> None:
    net = _netlist(tmp_path, load_example("rf_coupon_50r"))
    board = tmp_path / "build" / "coupon_v2.kicad_pcb"
    out = pr.write_artifact(net, board_path=board, fd="off")
    assert out["ok"] is True and out["errors"] == []
    assert Path(out["path"]) == tmp_path / "netlist.stackup.json"
    assert Path(out["dru_path"]) == tmp_path / "build" / "coupon_v2.kicad_dru"
    assert not (tmp_path / "netlist.kicad_dru").exists()            # never the netlist stem
    art = pr.read_artifact(out["path"])
    assert art["dru"]["path"] == "build/coupon_v2.kicad_dru"
    text = Path(out["dru_path"]).read_text(encoding="utf-8")
    assert dru_writer.BEGIN_MARKER in text and drc_canary_rule() in text
    assert "kicad_eda_plane_In1.Cu" in text and "kicad_eda_width_RF_IN_F.Cu" in text
    block = text[text.index(dru_writer.BEGIN_MARKER):]
    assert hashlib.sha256(block.encode("utf-8")).hexdigest() == art["dru"]["sha256"]
    assert out["dru"]["rules"] == art["dru"]["rules"]
    assert out["fd"]["mode"] == "off"


def drc_canary_rule() -> str:
    import drc_guard
    return drc_guard.CANARY_RULE


def test_default_board_is_the_netlist_stem(tmp_path: Path) -> None:
    net = _netlist(tmp_path, _masked_50r(), "node.json")
    out = pr.write_artifact(net, fd="off")
    assert Path(out["dru_path"]) == tmp_path / "node.kicad_dru"
    assert Path(out["path"]) == tmp_path / "node.stackup.json"
    assert pr.default_board(net) == tmp_path / "node.kicad_pcb"


def test_a_board_path_that_is_not_a_kicad_pcb_is_refused(tmp_path: Path) -> None:
    net = _netlist(tmp_path, _masked_50r())
    with pytest.raises(ValueError, match="kicad_pcb"):
        pr.write_artifact(net, board_path=tmp_path / "netlist.json", fd="off")
    assert not (tmp_path / "netlist.stackup.json").exists()


def test_two_runs_are_byte_identical(tmp_path: Path) -> None:
    net = _netlist(tmp_path, load_example("rf_lna_gainblock"))
    first = pr.write_artifact(net, fd="off")
    a = Path(first["path"]).read_bytes()
    d = Path(first["dru_path"]).read_bytes()
    second = pr.write_artifact(net, fd="off")
    assert first["changed"] is True and second["changed"] is False
    assert hashlib.sha256(Path(second["path"]).read_bytes()).hexdigest() == \
        hashlib.sha256(a).hexdigest()
    assert Path(second["dru_path"]).read_bytes() == d
    again = pr.artifact_text(pr.resolve_design(load_example("rf_lna_gainblock"), net, fd="off"))
    assert again.encode("ascii") == a


def test_user_rules_outside_the_markers_stay_byte_identical(tmp_path: Path) -> None:
    net = _netlist(tmp_path, _usb_design())
    user = "(version 1)\r\n# mine\r\n(rule \"keep\" (constraint clearance (min 0.2mm)))\r\n"
    (tmp_path / "netlist.kicad_dru").write_bytes(user.encode("utf-8"))
    out = pr.write_artifact(net, fd="off")
    text = Path(out["dru_path"]).read_bytes().decode("utf-8")
    assert text.startswith(user) and dru_writer.END_MARKER in text


def test_a_rules_file_that_cannot_be_rewritten_fails_the_run_not_the_artifact(
        tmp_path: Path) -> None:
    net = _netlist(tmp_path, _usb_design())
    (tmp_path / "netlist.kicad_dru").write_bytes(b"(version 1)\n# \xff\xfe latin-1 \xe9\n")
    out = pr.write_artifact(net, fd="off")
    assert out["ok"] is False and out["dru"] is None
    assert any(e.startswith("DRU not written") and "UTF-8" in e for e in out["errors"])
    assert Path(out["path"]).is_file()                        # the design itself is fine
    assert not pr.is_fresh(out["path"], _usb_design(), board_path=pr.default_board(net))


def test_a_decision_log_failure_is_reported(tmp_path: Path, monkeypatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> dict:
        raise decisions.DecisionError("could not lock the decision log")

    monkeypatch.setattr(decisions, "append_decision", refuse)
    out = pr.write_artifact(_netlist(tmp_path, load_example("cp2102n_uart")), fd="off")
    assert out["ok"] is False and out["decisions_written"] == 0
    assert sum("decision not recorded" in e for e in out["errors"]) == 2


def test_a_stackup_error_writes_the_artifact_but_no_dru(tmp_path: Path) -> None:
    net = _netlist(tmp_path, _fixture("st_e1")["design"])
    out = pr.write_artifact(net, fd="off")
    assert out["ok"] is False and any(e.startswith("ST-E1") for e in out["errors"])
    assert Path(out["path"]).is_file() and out["dru"] is None
    assert not Path(out["dru_path"]).exists()


def test_a_net_name_the_dru_cannot_quote_is_an_error(tmp_path: Path) -> None:
    design = {"nets": {"GND": [], "+3V3": [], "RF'X": []},
              "net_specs": {"RF'X": {"impedance": 50, "layers": ["F.Cu"]}}}
    net = _netlist(tmp_path, design)
    out = pr.write_artifact(net, fd="off")
    art = pr.read_artifact(out["path"])
    assert "error" in art["dru"] and any(e.startswith("DRU:") for e in art["errors"])
    assert out["ok"] is False and not Path(out["dru_path"]).exists()


def test_an_impedance_target_on_a_plane_net_is_an_error(tmp_path: Path) -> None:
    design = {"nets": {"GND": [], "+3V3": []}, "net_specs": {"GND": {"impedance": 50}}}
    art = pr.resolve_design(design, tmp_path / "netlist.json", fd="off")
    assert "GND" not in art["dsn"]["net_layers"]
    assert any("GND" in e and "plane net" in e for e in art["errors"])


# ---------------------------------------------------------------- decisions
def _log(net: Path) -> list[dict]:
    return decisions.read_decisions(net)


def test_drafts_are_recorded_once_across_runs(tmp_path: Path) -> None:
    design = load_example("cp2102n_uart")                 # In1 GND / In2 rail defaulted: 2 drafts
    net = _netlist(tmp_path, design)
    first = pr.write_artifact(net, fd="off")
    assert first["decisions_written"] == 2 and len(_log(net)) == 2
    assert [e["stage"] for e in _log(net)] == ["G1", "G1"]
    second = pr.write_artifact(net, fd="off")
    assert second["decisions_written"] == 0 and len(_log(net)) == 2
    changed = {**design, "net_specs": {**design["net_specs"],
                                       "USB_DP": {"diff": 90, "diff_pair": "USB_DM",
                                                  "layers": ["F.Cu"]}}}
    net.write_text(json.dumps(changed), encoding="utf-8")
    third = pr.write_artifact(net, fd="off")              # a new G3 draft (class C, fd off)
    assert third["decisions_written"] == 1
    log = _log(net)
    assert len(log) == 3 and log[-1]["stage"] == "G3"
    assert log[-1]["override"] == "net_specs.USB_DP.width"


def test_budget_zero_gives_a_closed_form_line_and_a_draft(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(fieldsolve2d.BUDGET_ENV, "0")
    net = _netlist(tmp_path, _masked_50r())
    out = pr.write_artifact(net, fd="auto")
    art = pr.read_artifact(out["path"])
    rec = art["lines"]["RF"]
    assert rec["accuracy_class"] == "B" and rec["status"] == "closed_form" and rec["fd"] is None
    g3 = [d for d in art["decision_drafts"] if d["stage"] == "G3"]
    assert len(g3) == 1 and "budget" in g3[0]["rationale"] and g3[0]["confidence"] == "low"
    assert out["fd"]["budget_s"] == 0 and out["fd"]["spent_s"] == 0 and out["fd"]["computed"] == 0
    assert [e for e in _log(net) if e["stage"] == "G3"]


def test_make_budget_follows_the_environment(monkeypatch) -> None:
    assert pr.make_budget("off") is None
    monkeypatch.setenv(fieldsolve2d.BUDGET_ENV, "12")
    assert pr.make_budget("auto").seconds == 12
    given = fieldsolve2d.FdBudget(3)
    assert pr.make_budget("force", given) is given
    with pytest.raises(ValueError, match="fd"):
        pr.make_budget("sometimes")


def test_fd_results_are_cached_in_the_artifact_and_reused(tmp_path: Path, monkeypatch) -> None:
    if fieldsolve2d.available() == "none":
        pytest.skip("no FD backend (numpy/scipy) installed")
    monkeypatch.setenv(fieldsolve2d.BUDGET_ENV, "300")
    net = _netlist(tmp_path, _masked_50r())
    first = pr.write_artifact(net, fd="auto")
    art = pr.read_artifact(first["path"])
    assert art["lines"]["RF"]["status"] == "fd" and art["lines"]["RF"]["fd"]["verified"] is True
    assert first["fd"]["computed"] >= 1 and first["fd"]["spent_s"] > 0
    second = pr.write_artifact(net, fd="auto")
    assert second["fd"]["computed"] == 0 and second["fd"]["spent_s"] == 0
    assert second["fd"]["reused"] == first["fd"]["computed"]
    assert second["changed"] is False
    assert impedance.fd_cache(art["lines"])


# ---------------------------------------------------------------- freshness
def test_design_digest_covers_the_physical_keys_only() -> None:
    base = load_example("rf_lna_gainblock")
    digest = pr.design_digest(base)
    assert pr.design_digest(copy.deepcopy(base)) == digest
    reordered = {**base, "components": list(reversed(base["components"]))}
    assert pr.design_digest(reordered) == digest
    assert pr.design_digest({**base, "title": "renamed"}) == digest
    value = copy.deepcopy(base)
    value["components"][0]["value"] = "other"
    assert pr.design_digest(value) == digest
    spec = copy.deepcopy(base)
    spec["net_specs"]["RF_IN"]["impedance"] = 75
    moved = copy.deepcopy(base)
    moved["components"][0]["side"] = "B"
    rail = {**base, "nets": {"+5V": [], **base["nets"]}}      # D7.2.3 first rail: names count
    for changed in (spec, moved, rail, {**base, "copper_layers": 2}, {**base, "mode": "napkin"},
                    {**base, "planes": {"In1.Cu": "GND"}}, {**base, "layer_policy": {}}):
        assert pr.design_digest(changed) != digest


def test_data_version_tracks_the_material_bytes(tmp_path: Path) -> None:
    v = pr.data_version()
    assert v == pr.data_version() and len(v) == HEX64
    root = tmp_path / "materials"
    shutil.copytree(mat.DEFAULT_DIR, root)
    first = sorted(root.glob("*.json"))[0]
    first.write_bytes(first.read_bytes() + b"\n")
    assert pr.data_version(materials=mat.load_library(root)) != v


def test_is_fresh_flips_on_net_spec_material_bytes_board_and_dru(tmp_path: Path) -> None:
    design = load_example("rf_coupon_50r")
    net = _netlist(tmp_path, design)
    out = pr.write_artifact(net, fd="off")
    path = out["path"]
    assert pr.is_fresh(path, design) and pr.staleness(path, design) is None
    assert pr.is_fresh(path, design, board_path=pr.default_board(net))
    assert pr.is_fresh(pr.read_artifact(path), design)
    spec = copy.deepcopy(design)
    spec["net_specs"]["RF_OUT"]["impedance"] = 75
    assert not pr.is_fresh(path, spec) and "digest" in pr.staleness(path, spec)
    root = tmp_path / "materials"
    shutil.copytree(mat.DEFAULT_DIR, root)
    first = sorted(root.glob("*.json"))[0]
    first.write_bytes(first.read_bytes() + b" ")
    lib = mat.load_library(root)
    assert not pr.is_fresh(path, design, materials=lib)
    assert "data" in pr.staleness(path, design, materials=lib)
    other = tmp_path / "other.kicad_pcb"
    assert "other.kicad_dru" in pr.staleness(path, design, board_path=other)
    dru = Path(out["dru_path"])
    dru.write_text(dru.read_text(encoding="utf-8").replace("min 10mm", "min 9mm"),
                   encoding="utf-8")
    assert "managed" in pr.staleness(path, design, board_path=pr.default_board(net))
    dru.unlink()
    assert "missing" in pr.staleness(path, design, board_path=pr.default_board(net))
    assert not pr.is_fresh(tmp_path / "nothing.stackup.json", design)
    (tmp_path / "junk.stackup.json").write_text("{not json", encoding="utf-8")
    assert "unreadable" in pr.staleness(tmp_path / "junk.stackup.json", design)


def test_a_mirror_beside_a_board_elsewhere_is_fresh(tmp_path: Path) -> None:
    # D7-23a mirrors the artifact to <board stem>.stackup.json when the stems differ
    design = load_example("rf_coupon_50r")
    net = _netlist(tmp_path, design)
    board = tmp_path / "build" / "coupon.kicad_pcb"
    out = pr.write_artifact(net, board_path=board, fd="off")
    mirror = tmp_path / "build" / "coupon.stackup.json"
    shutil.copyfile(out["path"], mirror)
    assert pr.is_fresh(mirror, design, board_path=board)
    assert pr.is_fresh(out["path"], design, board_path=board)
    assert "missing" in pr.staleness(mirror, design, board_path=tmp_path / "x.kicad_pcb")


def test_an_unreadable_previous_artifact_is_replaced(tmp_path: Path) -> None:
    net = _netlist(tmp_path, load_example("cp2102n_uart"))
    (tmp_path / "netlist.stackup.json").write_text("{broken", encoding="utf-8")
    out = pr.write_artifact(net, fd="off")
    assert out["ok"] and out["previous"] and "unreadable" in out["previous"]
    assert pr.read_artifact(out["path"])["schema"] == pr.SCHEMA
    assert out["decisions_written"] == 2


def test_read_artifact_refuses_other_json(tmp_path: Path) -> None:
    other = tmp_path / "x.stackup.json"
    other.write_text(json.dumps({"schema": "x@1"}), encoding="utf-8")
    with pytest.raises(ValueError, match="physical@1"):
        pr.read_artifact(other)


# ---------------------------------------------------------------- CLI
def test_cli_main_writes_and_reports(tmp_path: Path, capsys) -> None:
    net = _netlist(tmp_path, load_example("cp2102n_uart"))
    board = tmp_path / "out" / "uart.kicad_pcb"
    assert pr.main([str(net), "--board", str(board), "--fd", "off"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] and Path(report["dru_path"]) == tmp_path / "out" / "uart.kicad_dru"
    assert (tmp_path / "netlist.stackup.json").is_file()
    assert pr.main([str(tmp_path / "missing.json")]) == 2
    bad = _netlist(tmp_path, _fixture("st_e2")["design"], "bad.json")
    assert pr.main([str(bad), "--fd", "off"]) == 1


def test_cli_script_on_a_scratch_copy_of_cp2102n_uart(tmp_path: Path) -> None:
    work = tmp_path / "cp2102n_uart"
    shutil.copytree(ROOT / "examples" / "cp2102n_uart", work)
    board = work / "build" / "cp2102n.kicad_pcb"
    cmd = [sys.executable, str(SCRIPT), str(work / "netlist.json"), "--board", str(board),
           "--fd", "off"]
    runs = [subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                           stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace",
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            for _ in range(2)]
    shas = []
    for r in runs:
        assert r.returncode == 0, r.stderr[-2000:]
        assert json.loads(r.stdout)["ok"] is True
        shas.append(hashlib.sha256((work / "netlist.stackup.json").read_bytes()).hexdigest())
    assert shas[0] == shas[1]
    assert (work / "build" / "cp2102n.kicad_dru").is_file()
    assert not (work / "netlist.kicad_dru").exists()
    assert len(decisions.read_decisions(work / "netlist.json")) == 2
