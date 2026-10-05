"""server/tools/faithful.py: the D4 faithful-rebuild rules.

Verdicts are built with the real gates functions (g2_schematic, g3_placement,
g4_layout) and the J*-edge texts with the swig_gate_checks formats, so a
message drift in either breaks these tests.

Fixtures (tests/fixtures/faithful/):
* drc_ecc83.json / erc_ecc83.json: kicad-cli 9.0.4 on the ecc83 demo.
* drc_mixed.json / drc_mixed_all.json: kicad-cli 9.0.4 DRC (--severity-error
  / --severity-all) of mixed_board.sexpr (a .kicad_pcb, renamed like the
  equiv fixtures), built by make_drc_board.py: a trace jumper JP1 shorting
  its own pads, R1/R2 courtyards overlapping, a SIG track 0.05 mm from R3.1
  and one unrouted pair.
* erc_baseline_k9.json / erc_rebuilt_k9.json: kicad-cli 9.0.4 ERC of the
  ecc83 schematic with one and with three wires removed.
* drc_kinds_synthetic.json / erc_k11_synthetic.json: hand-written reports in
  the KiCad 9 / 10.99 item formats for the cases no real board shows.
"""
from __future__ import annotations

import copy
import importlib
import json
import shutil
import subprocess
import sys
import types
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

import faithful as fa
import gates
import net_equivalence as ne

if TYPE_CHECKING:
    from kicad_env import KiCadInstall

FIX =Path(__file__).resolve().parent / "fixtures" / "faithful"
EQUIV = Path(__file__).resolve().parent / "fixtures" / "equiv"
NETLIST = Path("C:/proj/rebuild/netlist.json")
NM = 1_000_000


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _design(**over: Any) -> dict:
    base = {"name": "rb", "source": {"kind": "kicad", "path": "src/a.kicad_pro",
                                     "reference": "ref/padnets.json",
                                     "erc_baseline": "${KIPRJMOD}/ref/erc.json"},
            "rebuild": "keep",
            "components": [{"ref": "R1", "at": [1, 2, 0]}, {"ref": "R2", "x": 3, "y": 4},
                           {"ref": "H1", "at": [5, 5, 0], "board_only": True}],
            "nets": {"A": [["R1", "1"], ["R2", "1"]]},
            "variant": {"mode": "keep", "dropped_refs": ["LOGO1"]}}
    return {**base, **over}


KEEP = fa.profile(_design(), NETLIST)
REFLOW = fa.profile(_design(rebuild="reflow"), NETLIST)
INACTIVE = fa.profile({"components": [], "nets": {}}, NETLIST)


# ----------------------------------------------------------------------------
# profile / pack_scales
# ----------------------------------------------------------------------------
def test_profile_is_inactive_without_source() -> None:
    assert INACTIVE == fa.INACTIVE and not INACTIVE.active
    assert fa.profile(_design(source=None), None) == fa.INACTIVE


def test_profile_reads_the_source_block() -> None:
    p = KEEP
    assert (p.active, p.kind, p.rebuild, p.all_fixed) == (True, "kicad", "keep", True)
    assert p.reference == NETLIST.parent / "ref" / "padnets.json"
    assert p.erc_baseline == NETLIST.parent / "ref" / "erc.json"        # ${KIPRJMOD}
    assert p.board_only == frozenset({"H1"}) and p.ignore_refs == frozenset({"LOGO1"})
    assert p.notes == ()
    assert p.as_dict()["reference"] == str(NETLIST.parent / "ref" / "padnets.json")


def test_profile_keeps_absolute_paths_and_relative_ones_without_a_netlist_path(
        tmp_path: Path) -> None:
    ref = tmp_path / "frozen.json"
    p = fa.profile(_design(source={"kind": "kicad", "reference": str(ref)}), NETLIST)
    assert p.reference == ref and p.erc_baseline is None
    assert any("source.erc_baseline" in n for n in p.notes)
    q = fa.profile(_design(), None)
    assert q.reference == Path("ref/padnets.json")


@pytest.mark.parametrize("kind", ["vision", "gerber", "kicad"])
def test_profile_without_reference_compares_against_the_netlist_itself(kind: str) -> None:
    p = fa.profile(_design(source={"kind": kind}, rebuild="reflow"), NETLIST)
    assert p.active and p.reference is None and p.erc_baseline is None
    assert any("netlist itself" in n for n in p.notes)


def test_profile_defaults_rebuild_to_reflow_with_a_note() -> None:
    design = _design()
    del design["rebuild"]
    p = fa.profile(design, NETLIST)
    assert p.rebuild == "reflow" and any("rebuild" in n for n in p.notes)


def test_profile_all_fixed_needs_every_part_placed() -> None:
    loose = _design(components=[{"ref": "R1", "at": [1, 2, 0]}, {"ref": "R2", "x": 3}])
    assert not fa.profile(loose, NETLIST).all_fixed
    assert not fa.profile(_design(components=[]), NETLIST).all_fixed


@pytest.mark.parametrize("design, match", [
    (_design(source="kicad"), "'source' must be an object"),
    (_design(rebuild="move"), "rebuild must be one of"),
    (_design(source={"kind": "kicad", "reference": 3}), "source.reference must be a path"),
    (_design(variant={"dropped_refs": "LOGO1"}), "dropped_refs must be a list"),
    (_design(variant=["LOGO1"]), "'variant' must be an object"),
    (_design(components={"R1": {}}), "'components' must be a list"),
    ("netlist", "design must be an object"),
])
def test_profile_rejects_malformed_input(design: Any, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        fa.profile(design, NETLIST)


def test_profile_does_not_mutate_the_design() -> None:
    design = _design()
    before = copy.deepcopy(design)
    fa.profile(design, NETLIST)
    assert design == before


def test_pack_scales() -> None:
    default = (1.0, 1.5, 2.0)
    assert fa.pack_scales(KEEP, default) == (1.0,)
    assert fa.pack_scales(REFLOW, default) is default
    loose = fa.profile(_design(components=[{"ref": "R1"}]), NETLIST)
    assert fa.pack_scales(loose, default) is default
    assert fa.pack_scales(INACTIVE, default) is default


# ----------------------------------------------------------------------------
# ERC baseline
# ----------------------------------------------------------------------------
def test_erc_keys_of_a_real_kicad9_report() -> None:
    assert fa.erc_keys(_load("erc_rebuilt_k9.json")) == Counter({
        ("pin_not_connected", ("#FLG(pwr)",)): 1, ("pin_not_connected", ("U1.6",)): 1,
        ("pin_not_connected", ("#PWR(GND)",)): 1, ("pin_not_connected", ("P2.2",)): 1,
        ("power_pin_not_driven", ("#PWR(GND)",)): 1})


def test_erc_keys_of_kicad_10_99_formats() -> None:
    keys = fa.erc_keys(FIX / "erc_k11_synthetic.json")
    assert keys == Counter({
        ("power_pin_not_driven", ("#PWR(P3.3V)",)): 1, ("pin_not_connected", ("U11P.5",)): 1,
        ("pin_not_connected", ("R1.1",)): 2, ("pin_to_pin", ("C6.2", "C7.1")): 1,
        ("wire_dangling", ()): 1, ("label_dangling", ()): 1})
    every = fa.erc_keys(FIX / "erc_k11_synthetic.json", severities=None)
    assert every[("lib_symbol_issues", ("#PWR(P3.3V_BAR)",))] == 1
    assert every[("footprint_link_issues", ("C13P",))] == 1


def test_erc_new_against_a_real_baseline() -> None:
    new = fa.erc_new(FIX / "erc_rebuilt_k9.json", FIX / "erc_baseline_k9.json")
    assert new == ["pin_not_connected: #FLG(pwr)", "pin_not_connected: U1.6",
                   "power_pin_not_driven: #PWR(GND)"]
    assert fa.erc_new(_load("erc_baseline_k9.json"), _load("erc_rebuilt_k9.json")) == []
    assert fa.erc_new(FIX / "erc_ecc83.json", FIX / "erc_ecc83.json") == []


def test_erc_new_is_a_multiset_difference_that_ignores_power_symbol_numbering() -> None:
    rebuilt = _load("erc_k11_synthetic.json")
    baseline = copy.deepcopy(rebuilt)
    sheet = baseline["sheets"][0]["violations"]
    sheet[0]["items"][0]["description"] = "Symbol #PWR07 Hidden pin 1 [P3.3V, Power input, Line]"
    del baseline["sheets"][1]["violations"][0]                 # R1.1 twice -> once
    del sheet[4]                                               # the dangling wire
    assert fa.erc_new(rebuilt, baseline) == ["pin_not_connected: R1.1",
                                             "wire_dangling (no symbol)"]
    assert fa.erc_new(rebuilt, {"sheets": []}) == [
        "label_dangling (no symbol)", "pin_not_connected: R1.1 (x2)",
        "pin_not_connected: U11P.5", "pin_to_pin: C6.2, C7.1",
        "power_pin_not_driven: #PWR(P3.3V)", "wire_dangling (no symbol)"]


def test_erc_new_accepts_precomputed_keys_and_skips_generator_types(
        monkeypatch: pytest.MonkeyPatch) -> None:
    assert fa.GENERATOR_ERC_TYPES == ()
    rebuilt = fa.erc_keys(FIX / "erc_k11_synthetic.json")
    monkeypatch.setattr(fa, "GENERATOR_ERC_TYPES", ("wire_dangling", "label_dangling"))
    new = fa.erc_new(rebuilt, Counter())
    assert not any(n.startswith(("wire_dangling", "label_dangling")) for n in new)
    assert len(new) == 4


@pytest.mark.parametrize("report, err", [
    (["not", "a", "report"], TypeError), ({"sheets": "x"}, ValueError),
    ({"sheets": [{"violations": [3]}]}, ValueError)])
def test_erc_keys_rejects_malformed_reports(report: Any, err: type[Exception]) -> None:
    with pytest.raises(err):
        fa.erc_keys(report)


@pytest.mark.parametrize("text, err", [("[1, 2]", "is not a JSON object"),
                                       ("{oops", "is not JSON")])
def test_erc_keys_reads_a_bad_file_loudly(tmp_path: Path, text: str, err: str) -> None:
    bad = tmp_path / "erc.json"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=f"ERC report .* {err}"):
        fa.erc_keys(bad)


@pytest.mark.parametrize("text, token", [
    ("Symbol U2 Pin 23 [SCL, Input, Line]", "U2.23"),
    ("Symbol USB1 Pin B5 [CC2, Unspecified, Line]", "USB1.B5"),
    ("Symbol R1 Pin 1 [Passive, Line]", "R1.1"),
    ("Symbol #PWR01 Hidden pin 1 [GND, Power input, Line]", "#PWR(GND)"),
    ("Symbol #PWR? Hidden pin  [P3.3V, Power input, Line]", "#PWR(P3.3V)"),
    ("Symbol #FLG05 Pin 1 [pwr, Power output, Line]", "#FLG(pwr)"),
    ("Symbol U9 Hidden pin  [VDD, Power input, Line]", "U9.VDD"),
    ("Symbol A [Unknown_0_-806]", "A"),
    ("Symbol #PWR? [P3.3V_BAR]", "#PWR(P3.3V_BAR)"),
    ("Field Value of Symbol U5", "U5"),
    ("Field Value of Symbol #PWR03", "#PWR()"),
    ("Vertical Wire, length 0.0127 mm", None),
    ("Global Label 'SDA'", None),
    ("No Connect", None),
])
def test_erc_token(text: str, token: str | None) -> None:
    assert fa.erc_token(text) == token


# ----------------------------------------------------------------------------
# relax_g2 (verdicts from gates.g2_schematic)
# ----------------------------------------------------------------------------
DESIGN_REP = {"decoupling": {"uncovered": [{"ref": "U1", "rails_without_cap": ["+5V", "+3V3"]}]},
              "flags": {"floating_pins": ["U2.3"]},
              "errors": ["U9: symbol not found"]}
THERMAL = {"blockers": ["U4 dissipates 2 W"], "warnings": []}


def _g2(erc: int | None = 4) -> dict:
    return gates.g2_schematic({"ok": True, "erc_violations": erc}, DESIGN_REP, THERMAL)


def test_relax_g2_turns_decoupling_into_warnings_and_drops_its_auto_tasks() -> None:
    g2 = _g2(erc=0)
    before = copy.deepcopy(g2)
    out = fa.relax_g2(g2, new_erc=[], erc_total=0, profile=KEEP)
    assert g2 == before                                        # never mutated
    assert not any("bypass cap" in b for b in out["blockers"])
    assert out["blockers"] == [
        "thermal: U4 dissipates 2 W", "design review could not run: U9: symbol not found",
        "floating pin (not netted, not no_connect): U2.3"]
    assert out["warnings"][-2:] == [
        "faithful: the source design has no bypass on +5V at U1 (kept as designed)",
        "faithful: the source design has no bypass on +3V3 at U1 (kept as designed)"]
    assert not any(t["fix"].get("type") == "add_decoupling" for t in out["tasks"])
    assert len(out["tasks"]) == len(g2["tasks"]) - 2 and out["passed"] is False


def test_relax_g2_passes_when_only_decoupling_and_baseline_erc_remain() -> None:
    g2 = gates.g2_schematic({"ok": True, "erc_violations": 2},
                            {"decoupling": DESIGN_REP["decoupling"]}, None)
    assert not g2["passed"]
    out = fa.relax_g2(g2, new_erc=[], erc_total=2, profile=KEEP)
    assert out["passed"] is True and out["blockers"] == [] and out["tasks"] == []
    assert "faithful: ERC reports 2 violation(s), none new against the source baseline" \
        in out["warnings"]


def test_relax_g2_blocks_on_new_erc_violations_in_place_of_the_count() -> None:
    new = [f"pin_not_connected: U1.{i}" for i in range(12)]
    out = fa.relax_g2(_g2(erc=14), new_erc=new, erc_total=14, profile=KEEP)
    erc = [b for b in out["blockers"] if b.startswith("ERC")]
    assert erc == ["ERC: 12 violation(s) not in the source baseline: "
                   + "; ".join(new[:10]) + " (+2 more)"]
    assert out["blockers"].index(erc[0]) == 1                  # where the count blocker was
    descs = [t["desc"] for t in out["tasks"]]
    assert "resolve 12 ERC violation(s) not in the source baseline" in descs
    assert not any(d == "resolve 14 ERC violation(s)" for d in descs)


def test_relax_g2_adds_the_new_erc_blocker_even_without_a_count_blocker() -> None:
    out = fa.relax_g2(_g2(erc=0), new_erc=["wire_dangling (no symbol)"], erc_total=0,
                      profile=KEEP)
    assert "ERC: 1 violation(s) not in the source baseline: wire_dangling (no symbol)" \
        in out["blockers"]


def test_relax_g2_without_a_baseline_keeps_the_erc_blocker_and_says_so() -> None:
    out = fa.relax_g2(_g2(erc=3), new_erc=None, erc_total=3, profile=KEEP)
    assert "ERC reports 3 violation(s)" in out["blockers"]
    assert fa.NO_ERC_BASELINE in out["warnings"]
    clean = fa.relax_g2(_g2(erc=0), new_erc=None, erc_total=0, profile=KEEP)
    assert fa.NO_ERC_BASELINE not in clean["warnings"]


def test_relax_g2_counts_from_the_blocker_when_no_total_is_given() -> None:
    out = fa.relax_g2(_g2(erc=5), new_erc=[], erc_total=None, profile=KEEP)
    assert "faithful: ERC reports 5 violation(s), none new against the source baseline" \
        in out["warnings"]


def test_relax_g2_without_decoupling_only_judges_erc() -> None:
    g2 = gates.g2_schematic({"ok": True, "erc_violations": 1}, {}, None)
    out = fa.relax_g2(g2, new_erc=["pin_not_connected: U1.6"], erc_total=1, profile=KEEP)
    assert out["blockers"] == ["ERC: 1 violation(s) not in the source baseline: "
                               "pin_not_connected: U1.6"]
    assert [t["desc"] for t in out["tasks"]] == [
        "resolve 1 ERC violation(s) not in the source baseline"]
    assert out["warnings"] == g2["warnings"] and out["passed"] is False


def test_relax_g2_is_an_identity_when_inactive() -> None:
    g2 = _g2()
    out = fa.relax_g2(g2, new_erc=[], erc_total=4, profile=INACTIVE)
    assert out == g2 and out is not g2


# ----------------------------------------------------------------------------
# relax_g3 (texts in the swig_gate_checks formats, verdicts from g3_placement)
# ----------------------------------------------------------------------------
INTO = "J1: mating face points INTO the board interior (unusable in an enclosure)"
FAR = f"J2: mating face is {7.25:.1f} mm from the board edge it faces (must be at the edge, <=3 mm)"
OTHER = "U1: dissipating exposed-pad part has only 0 thermal via(s) under the EP"


def test_relax_g3_downgrades_connector_edge_issues() -> None:
    issues, warnings = [INTO, OTHER, FAR], ["prior warning"]
    got_i, got_w = fa.relax_g3(issues, warnings, KEEP)
    assert got_i == [OTHER]
    assert got_w == ["prior warning",
                     f"faithful: {INTO} (connector kept where the source design has it)",
                     f"faithful: {FAR} (connector kept where the source design has it)"]
    assert issues == [INTO, OTHER, FAR] and warnings == ["prior warning"]
    g3 = gates.g3_placement({"ok": True}, {"courtyard_violations": 0}, *fa.relax_g3(
        [INTO, FAR], [], REFLOW))
    assert g3["passed"] and len(g3["warnings"]) == 2


def test_relax_g3_prefers_the_structured_code() -> None:
    coded = {"code": "connector_edge", "msg": "J5: off the edge (new wording)"}
    plain = {"code": "courtyard_outside", "msg": INTO}
    got_i, got_w = fa.relax_g3([coded, plain], [], KEEP)
    assert got_i == [plain] and got_w == [
        "faithful: J5: off the edge (new wording) (connector kept where the source design has it)"]
    got_i, got_w = fa.relax_g3([INTO, "J7: new text"], [], KEEP,
                               codes=["outline_open", "connector_edge"])
    assert got_i == [INTO] and len(got_w) == 1 and "J7: new text" in got_w[0]
    with pytest.raises(ValueError, match="codes"):
        fa.relax_g3([INTO], [], KEEP, codes=[])


def test_relax_g3_is_an_identity_when_inactive() -> None:
    issues, warnings = [INTO, OTHER], ["w"]
    got = fa.relax_g3(issues, warnings, INACTIVE)
    assert got == (issues, warnings) and got[0] is not issues


def _gate_checks() -> types.ModuleType:
    """swig_gate_checks; without pcbnew (the venv) a stub carrying ToMM and
    the layer ids is registered for the import only, as
    tests/test_gate_checks_interior.py does."""
    try:
        import pcbnew  # noqa: F401  (KiCad's bundled python only)
    except ImportError:
        stub = types.ModuleType("pcbnew")
        stub.ToMM = lambda nm: nm / NM
        stub.F_CrtYd, stub.B_CrtYd, stub.F_Cu, stub.B_Cu = 46, 47, 0, 2
        sys.modules["pcbnew"] = stub
        try:
            return importlib.import_module("swig_gate_checks")
        finally:
            if sys.modules.get("pcbnew") is stub:
                del sys.modules["pcbnew"]
    return importlib.import_module("swig_gate_checks")


def _vec(x: float, y: float) -> SimpleNamespace:
    return SimpleNamespace(x=round(x * NM), y=round(y * NM))


def _box(x0: float, y0: float, x1: float, y1: float) -> SimpleNamespace:
    return SimpleNamespace(GetLeft=lambda: round(x0 * NM), GetTop=lambda: round(y0 * NM),
                           GetRight=lambda: round(x1 * NM), GetBottom=lambda: round(y1 * NM),
                           GetCenter=lambda: _vec((x0 + x1) / 2, (y0 + y1) / 2))


def _connector(ref: str, pad_y: float, box: tuple) -> SimpleNamespace:
    x = (box[0] + box[2]) / 2
    pads = [SimpleNamespace(GetPosition=lambda dx=dx: _vec(x + dx, pad_y)) for dx in (-1, 1)]
    yard = SimpleNamespace(OutlineCount=lambda: 1, BBox=lambda: _box(*box))
    return SimpleNamespace(GetReference=lambda: ref, Pads=lambda: pads,
                           GetCourtyard=lambda _layer: yard)


def test_relax_g3_matches_the_texts_swig_gate_checks_emits() -> None:
    """Drift guard: the real check_placement on a fake 40 x 30 board (J1 at
    the top edge facing inward, J2 facing the top edge from 6 mm away)."""
    fps = [_connector("J1", 4.0, (17.0, 3.0, 23.0, 9.0)),
           _connector("J2", 12.0, (7.0, 6.0, 13.0, 13.0))]
    board = SimpleNamespace(GetBoardEdgesBoundingBox=lambda: _box(0.0, 0.0, 40.0, 30.0),
                            GetFootprints=lambda: fps, Zones=lambda: [])
    issues, _ = _gate_checks().check_placement(board, {"components": [{"ref": "J1"},
                                                                      {"ref": "J2"}]})
    assert [i.split(":")[0] for i in issues] == ["J1", "J2"]
    kept, warnings = fa.relax_g3(issues, [], KEEP)
    assert kept == [] and warnings == [f"faithful: {i} {fa.KEPT_CONNECTOR}" for i in issues]
    assert fa.relax_g3(issues, [], INACTIVE) == (issues, [])


# ----------------------------------------------------------------------------
# split_drc / courtyard_view (verdicts from g3_placement / g4_layout)
# ----------------------------------------------------------------------------
def _g4(split: fa.DrcSplit) -> dict:
    drc = {"errors": split.ours_errors, "types": split.ours_types,
           "unconnected": split.unconnected}
    return gates.g4_layout({"unconnected": 0}, drc, [], split.placement + split.intrinsic)


def test_keep_mode_leaves_only_the_track_clearance_as_a_blocker() -> None:
    split = fa.split_drc(_load("drc_mixed.json"), KEEP)
    assert (split.ours_errors, split.ours_types, split.unconnected) == (1, {"clearance": 1}, 1)
    assert split.ours == ["DRC clearance: Clearance violation (netclass 'Default' clearance "
                          "0.2000 mm; actual 0.0500 mm) [Pad 1 [OTHER] of R3 on F.Cu; "
                          "Track [SIG] on F.Cu, length 8.0000 mm]"]
    assert split.placement == ["DRC courtyards_overlap from the source placement: Courtyards "
                               "overlap [Footprint R1; Footprint R2]"]
    assert len(split.intrinsic) == 4
    assert all(m.startswith("DRC ") and " inside JP1 (intrinsic to the footprint): " in m
               for m in split.intrinsic)
    g4 = _g4(split)
    assert [b for b in g4["blockers"] if "violation" in b] == ["DRC: 1 violation(s) (clearancex1)"]
    assert "DRC: 1 unconnected item(s)" in g4["blockers"]
    assert len(g4["warnings"]) == 5
    cy = fa.courtyard_view(split)
    assert cy == {"courtyard_violations": 0, "types": {"clearance": 1}}
    assert gates.g3_placement({"ok": True}, cy, [])["passed"]


def test_reflow_mode_keeps_the_courtyard_overlap_as_a_blocker() -> None:
    split = fa.split_drc(_load("drc_mixed.json"), REFLOW)
    assert split.ours_types == {"clearance": 1, "courtyards_overlap": 1}
    assert (split.ours_errors, split.placement, len(split.intrinsic)) == (2, [], 4)
    cy = fa.courtyard_view(split)
    assert cy["courtyard_violations"] == 1
    g3 = gates.g3_placement({"ok": True}, cy, [])
    assert not g3["passed"] and g3["blockers"] == ["1 courtyard overlap(s) — parts collide"]
    assert "DRC: 2 violation(s) (clearancex1, courtyards_overlapx1)" in _g4(split)["blockers"]


def test_active_split_counts_errors_only() -> None:
    every = fa.split_drc(FIX / "drc_mixed_all.json", KEEP)
    assert every == fa.split_drc(FIX / "drc_mixed.json", KEEP)


@pytest.mark.parametrize("name", ["drc_mixed.json", "drc_mixed_all.json",
                                  "drc_kinds_synthetic.json", "drc_ecc83.json"])
def test_split_drc_is_an_identity_when_inactive(name: str) -> None:
    """Inactive = run_pipeline._drc / _courtyard_drc on the same report."""
    report = _load(name)
    types = dict(Counter(v["type"] for v in report["violations"]))
    split = fa.split_drc(report, INACTIVE)
    assert split == fa.DrcSplit(len(report["violations"]), types, [], [],
                                len(report["unconnected_items"]), split.ours)
    assert fa.courtyard_view(split) == {
        "courtyard_violations": sum(n for t, n in types.items() if "courtyard" in t),
        "types": types}


def test_synthetic_kinds_split_by_mode() -> None:
    keep = fa.split_drc(FIX / "drc_kinds_synthetic.json", KEEP)
    assert keep.ours_types == {"clearance": 3, "copper_edge_clearance": 1,
                               "items_not_allowed": 1, "mystery": 1, "malformed_courtyard": 1}
    assert [m.split(" ")[1] for m in keep.placement] == [
        "copper_edge_clearance", "hole_clearance", "invalid_outline"]
    assert keep.intrinsic == ["DRC shorting_items inside JP2 (intrinsic to the footprint): Items "
                              "shorting two nets (nets A and B) [Pad 1 [A] of JP2 on F.Cu; "
                              "Pad 2 [B] of JP2 on F.Cu]"]
    assert keep.unconnected == 2 and keep.ours_errors == 7
    reflow = fa.split_drc(FIX / "drc_kinds_synthetic.json", REFLOW)
    assert reflow.placement == [] and reflow.intrinsic == keep.intrinsic
    assert reflow.ours_errors == 10 and reflow.ours_types["invalid_outline"] == 1
    assert fa.courtyard_view(keep)["courtyard_violations"] == 1        # malformed_courtyard


@pytest.mark.parametrize("text, kind, ref, layer", [
    ("Track [SIG] on F.Cu, length 8.0000 mm", "track", None, "F.Cu"),
    ("Track (arc) [GND] on B.Cu, length 2.0000 mm", "arc", None, "B.Cu"),
    ("Via [GND] on F.Cu - B.Cu", "via", None, "F.Cu"),
    ("Blind/Buried Via [SIG] on F.Cu - In1.Cu", "via", None, "F.Cu"),
    ("Micro Via [SIG] on F.Cu - In1.Cu", "via", None, "F.Cu"),
    ("Zone [GND] on B.Cu, priority 0", "zone", None, "B.Cu"),
    ("Zone 'of U1' [GND] on F.Cu, B.Cu, priority 1", "zone", None, "F.Cu"),
    ("Teardrop [SIG] on F.Cu", "zone", None, "F.Cu"),
    ("Pad 2 [GND] of R1 on F.Cu", "footprint", "R1", "F.Cu"),
    ("Pad 1 [net of U9] of U2 on B.Cu", "footprint", "U2", "B.Cu"),
    ("PTH pad 1 [VCC] of J1", "footprint", "J1", None),
    ("NPTH pad of H1", "footprint", "H1", None),
    ("Segment [<no net>] of JP1 on F.Cu", "footprint", "JP1", "F.Cu"),
    ("Segment of P4 on F.Silkscreen", "footprint", "P4", "F.Silkscreen"),
    ("Reference field of U1", "footprint", "U1", None),
    ("Value field of R1 (10k)", "footprint", "R1", None),
    ("Footprint text of U1 (${REFERENCE})", "footprint", "U1", None),
    ("Footprint R2", "footprint", "R2", None),
    ("Segment on Edge.Cuts", "edge", None, "Edge.Cuts"),
    ("Arc on Edge.Cuts", "edge", None, "Edge.Cuts"),
    ("Segment [GND] on F.Cu", "board", None, "F.Cu"),
    ("Text 'made of R1' on F.Silkscreen", "board", None, "F.Silkscreen"),
    ("Dimension 'W' on Dwgs.User", "board", None, "Dwgs.User"),
    ("Rule Area 'ANT' on F.Cu, B.Cu", "board", None, "F.Cu"),
    ("Mystery item", "unknown", None, None),
    ("", "unknown", None, None),
])
def test_classify_item(text: str, kind: str, ref: str | None, layer: str | None) -> None:
    assert fa.classify_item(text) == fa.DrcItem(kind, ref, layer, text)


def test_every_real_drc_item_is_classified() -> None:
    for name in ("drc_ecc83.json", "drc_mixed_all.json"):
        report = _load(name)
        items = [i["description"] for v in report["violations"] + report["unconnected_items"]
                 for i in v["items"]]
        assert items and all(fa.classify_item(t).kind != "unknown" for t in items), name


def test_ecc83_drc_has_nothing_of_ours() -> None:
    split = fa.split_drc(FIX / "drc_ecc83.json", KEEP)
    assert split.as_dict() == {"ours_errors": 0, "ours_types": {}, "ours": [], "placement": [],
                               "intrinsic": [], "unconnected": 0}


@pytest.mark.parametrize("report, err", [
    ("not json at all", FileNotFoundError), (3, TypeError),
    ({"violations": {}}, ValueError), ({"violations": [{"items": "x"}]}, ValueError)])
def test_split_drc_rejects_malformed_reports(report: Any, err: type[Exception]) -> None:
    with pytest.raises(err):
        fa.split_drc(report, KEEP)


# ----------------------------------------------------------------------------
# equivalence_findings
# ----------------------------------------------------------------------------
def _reference() -> dict:
    return ne.netlist_pad_nets(json.loads((EQUIV / "netlist.json").read_text(encoding="utf-8")))


def test_equivalence_of_the_source_board_is_clean() -> None:
    board = ne.load_pad_nets_meta(EQUIV / "board_k9.sexpr")
    found = fa.equivalence_findings("G3", _reference(), board)
    assert found.blockers == [] and found.warnings == []
    assert found.summary["equal"] is True and found.summary["differences"] == []
    assert found.summary["summary"].startswith("equivalent")


def test_a_pad_on_another_net_is_a_blocker() -> None:
    ref = _reference()
    cand = {**ref, "EN": ref["EN"] - {("R2", "1")}, "GND": ref["GND"] | {("R2", "1")}}
    found = fa.equivalence_findings("G3", ref, cand)
    assert found.blockers == [
        "G3: connectivity differs from the source: NOT equivalent: 1 split, 1 merged, "
        "0 missing, 0 extra pin(s): source net 'EN' is split here into EN: R1.2 U1.1 | "
        "GND: R2.1; net 'GND' here joins source nets GND: C1.2 H1.1 J1.2 R2.2 U1.EP | EN: R2.1"]
    blockers, warnings, summary = found
    assert summary["equal"] is False and len(summary["differences"]) == 2
    assert summary["stage"] == "G3" and summary["differences_total"] == 2


def test_missing_and_extra_pads_and_ignore_refs() -> None:
    ref = _reference()
    cand = {"VCC": ref["VCC"], "EN": ref["EN"], "A_B": ref["A_B"],
            "GND": ref["GND"] - {("H1", "1")}, "NEW": {("TP1", "1"), ("TP2", "1")}}
    found = fa.equivalence_findings("G2", ref, cand)
    assert found.summary["differences"] == ["missing here: H1.1 (source net GND)",
                                            "extra here: TP1.1 TP2.1 (net NEW)"]
    clean = fa.equivalence_findings("G2", ref, cand, ignore_refs={"H1", "TP1", "TP2"})
    assert clean.blockers == [] and clean.summary["ignored_refs"] == ["H1", "TP1", "TP2"]


def test_differences_are_capped_at_ten_in_the_blocker() -> None:
    ref = {f"N{i}": {(f"R{i}", "1"), (f"C{i}", "1")} for i in range(13)}
    cand = {f"M{i}": {(f"R{i}", "1"), (f"D{i}", "1")} for i in range(13)}
    found = fa.equivalence_findings("G4", ref, cand)
    assert found.summary["differences_total"] == 26
    assert len(found.summary["differences"]) == 10
    assert found.blockers[0].endswith(" (+16 more)")


def test_duplicate_refs_are_warnings() -> None:
    ref = _reference()
    meta = {"duplicate_refs": ["POLY"], "kind": "board"}
    found = fa.equivalence_findings("G4", (ref, {"duplicate_refs": []}), (ref, meta))
    assert found.blockers == [] and found.warnings == [
        "G4: reference POLY is shared by several parts of the candidate; their pads count "
        "as one part, so the comparison cannot tell them apart"]
    assert found.summary["duplicate_refs"] == {"reference": [], "candidate": ["POLY"]}
    two = fa.equivalence_findings("G3", (ref, {"duplicate_refs": ["", "STITCH1", "POLY"]}), ref,
                                  ignore_refs=["POLY"])
    assert two.warnings == [
        "G3: references (none), STITCH1 are shared by several parts of the reference; their "
        "pads count as one part, so the comparison cannot tell them apart"]


@pytest.mark.parametrize("bad", [["a"], ("x", "y"), None])
def test_equivalence_rejects_non_pad_nets(bad: Any) -> None:
    with pytest.raises(ValueError, match="pad nets"):
        fa.equivalence_findings("G2", bad, _reference())


# ----------------------------------------------------------------------------
# Live: kicad-cli 9 DRC (skips without a KiCad 9 install)
# ----------------------------------------------------------------------------
@pytest.fixture(scope="module")
def kicad9() -> KiCadInstall:
    import kicad_env as env
    install = next((i for i in env.discover() if i.version_tuple[0] == 9 and i.share), None)
    if install is None:
        pytest.skip("no KiCad 9.x install (kicad-cli 9 and its demos)")
    return install


def _drc(cli: Path, pcb: Path, out: Path, *flags: str) -> dict:
    r = subprocess.run([str(cli), "pcb", "drc", str(pcb), "--format", "json", "-o", str(out),
                        *flags], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", stdin=subprocess.DEVNULL, timeout=300)
    output = r.stdout + r.stderr
    assert r.returncode in (0, 5) and "Error:" not in output, output[-800:]
    return json.loads(out.read_text(encoding="utf-8"))


def test_live_ecc83_drc_items_are_all_classified(kicad9: KiCadInstall, tmp_path: Path) -> None:
    demo = kicad9.share / "demos" / "ecc83"
    if not (demo / "ecc83-pp.kicad_pcb").is_file():
        pytest.skip(f"ecc83 demo not installed under {kicad9.share}")
    copy_dir = tmp_path / "ecc83"
    shutil.copytree(demo, copy_dir)
    report = _drc(kicad9.kicad_cli, copy_dir / "ecc83-pp.kicad_pcb", tmp_path / "drc.json",
                  "--severity-all")
    items = [i["description"] for v in report["violations"] + report["unconnected_items"]
             for i in v["items"]]
    assert items, "the ecc83 demo reported nothing to classify"
    unknown = [t for t in items if fa.classify_item(t).kind == "unknown"]
    assert unknown == []
    assert fa.split_drc(report, KEEP).ours_errors == 0


def test_live_mixed_board_splits_like_the_committed_report(kicad9: KiCadInstall,
                                                         tmp_path: Path) -> None:
    shutil.copyfile(FIX / "mixed_board.sexpr", tmp_path / "mixed.kicad_pcb")
    report = _drc(kicad9.kicad_cli, tmp_path / "mixed.kicad_pcb", tmp_path / "drc.json",
                  "--severity-error")
    for prof in (KEEP, REFLOW):
        assert fa.split_drc(report, prof) == fa.split_drc(FIX / "drc_mixed.json", prof)


def test_live_rebuilt_fixture_board_splits_the_same(kicad9: KiCadInstall,
                                                    tmp_path: Path) -> None:
    """make_drc_board.py under KiCad 9's pcbnew (new UUIDs, so kicad-cli may
    order items differently) still splits exactly like the committed report."""
    if kicad9.python is None:
        pytest.skip("the KiCad 9 install has no bundled Python with pcbnew")
    pcb = tmp_path / "mixed.kicad_pcb"
    r = subprocess.run([str(kicad9.python), str(FIX / "make_drc_board.py"), str(pcb),
                        str(kicad9.share / "footprints")], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=300)
    assert r.returncode == 0 and pcb.is_file(), (r.stdout + r.stderr)[-800:]
    report = _drc(kicad9.kicad_cli, pcb, tmp_path / "drc.json", "--severity-error")
    for prof in (KEEP, REFLOW):
        assert fa.split_drc(report, prof) == fa.split_drc(FIX / "drc_mixed.json", prof)
