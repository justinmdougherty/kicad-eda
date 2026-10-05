"""server/tools/lint_rules_import.py (F46-18): G1 lint rules for imported /
rebuilt designs (IM-E1..IM-E4 errors, IM-W1..IM-W4 warnings), ORDER 40.

Pure tier: no KiCad. Every rule has a passing and a failing case; a design
without source / rebuild / variant gets no findings at all; lint_netlist
discovers the module and imported designs still lint ok.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import lint_netlist
import lint_rules_import as im
import net_equivalence as ne
from lint_context import LintContext

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "server" / "tools"
MODULE = TOOLS / "lint_rules_import.py"
K9, K10, K11 = 20241229, 20260206, 20260901


# --- fixtures -------------------------------------------------------------------------------
def _comp(ref: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"ref": ref, "lib_id": "Device:R", "value": "10k",
                            "footprint": "Resistor_SMD:R_0603"}
    return {**base, **extra}


def _hole(ref: str, **extra: Any) -> dict[str, Any]:
    return {"ref": ref, "board_only": True, "value": ref,
            "footprint": "MountingHole:MountingHole_3.2mm_M3", **extra}


def _write_source(directory: Path) -> tuple[Path, str]:
    path = directory / "orig" / "board.kicad_pro"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"meta": "source project"}', encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _write_reference(directory: Path, name: str = "reference_padnets.json") -> Path:
    nets = {"N1": {("R1", "1"), ("R2", "1")}, "N2": {("R1", "2"), ("R2", "2")}}
    return ne.write_reference(directory / name, nets, board_only=["H1"])


def _imported(directory: Path, **top: Any) -> dict[str, Any]:
    """A clean keep-mode import whose source files exist under `directory`."""
    src, sha = _write_source(directory)
    _write_reference(directory)
    design: dict[str, Any] = {
        "name": "imp",
        "source": {"kind": "kicad", "path": "orig/board.kicad_pro", "sha256": sha,
                   "tool": "kicad-cli 9.0.4", "reference": "reference_padnets.json"},
        "rebuild": "keep",
        "components": [_comp("R1", at=[1.0, 2.0, 0.0]), _comp("R2", at=[3.0, 2.0, 90.0]),
                       _hole("H1", at=[5.0, 5.0, 0.0])],
        "nets": {"N1": [["R1", "1"], ["R2", "1"]], "N2": [["R1", "2"], ["R2", "2"]]},
        "no_connect": [],
    }
    assert src.is_file()
    return {**design, **top}


def _ctx(directory: Path | None, **kw: Any) -> LintContext:
    return LintContext(netlist_dir=directory, **kw)


def _ids(findings: list[str]) -> list[str]:
    return [f.split(" ", 1)[0] for f in findings]


def _check(design: dict[str, Any], ctx: LintContext) -> tuple[list[str], list[str]]:
    before = copy.deepcopy(design)
    errors, warnings = im.check(design, ctx)
    assert design == before, "check() must not mutate the design"
    assert isinstance(errors, list) and isinstance(warnings, list)
    return errors, warnings


@pytest.fixture
def imported(tmp_path: Path) -> dict[str, Any]:
    return _imported(tmp_path)


# --- the clean cases ------------------------------------------------------------------------
def test_a_clean_import_has_no_findings(tmp_path: Path, imported: dict) -> None:
    assert _check(imported, _ctx(tmp_path)) == ([], [])


def test_designs_without_source_or_rebuild_produce_no_findings(tmp_path: Path) -> None:
    # every rule's trigger, but no source / rebuild / variant: not an import
    _footprint(tmp_path / "libs" / "New.pretty", "Part", K11)
    plain = {"components": [_comp("R1", dnp="yes"), _hole("H1")],
             "nets": {"N": [["R1", "1"], ["R1", "2"]]}, "no_connect": [["H1", "1"]],
             "libraries": {"footprints": {"New": "libs/New.pretty"}}}
    plain["components"][0]["footprint"] = "New:Part"
    assert _check(plain, _ctx(tmp_path)) == ([], [])
    assert _check({}, LintContext()) == ([], [])


@pytest.mark.parametrize("name", ["buck_5v3a", "rpi5_battery_hat", "lm358_buffer",
                                  "rf_coupon_50r", "stm32f103_dev"])
def test_example_designs_produce_no_findings(name: str) -> None:
    path = ROOT / "examples" / name / "netlist.json"
    design = json.loads(path.read_text(encoding="utf-8"))
    assert _check(design, _ctx(path.parent)) == ([], [])


def test_is_imported() -> None:
    assert not im.is_imported({"components": []})
    assert not im.is_imported({"source": None, "rebuild": None, "variant": None})
    for key, value in (("source", {"kind": "kicad", "path": "x"}), ("rebuild", "reflow"),
                       ("variant", {"mode": "keep"})):
        assert im.is_imported({key: value})


def test_a_malformed_design_never_raises(tmp_path: Path) -> None:
    weird = [
        {"source": "not a block", "rebuild": "keep", "components": "nope", "nets": []},
        {"source": {}, "rebuild": "keep", "components": [None, 3, {"no": "ref"}],
         "nets": {"N": "x", "M": [None, ["R1"], "ab"]}, "no_connect": "x"},
        {"variant": {"dropped_refs": "LOGO1"}, "components": [], "nets": None},
        {"variant": "keep", "rebuild": "reflow", "components": [{"ref": 5, "at": [1, 2, 0]}],
         "no_connect": [None, ["H1"], 7], "libraries": "x"},
        {"rebuild": "keep", "libraries": {"footprints": ["x"]}, "components": [
            {"ref": "U1", "footprint": 7, "at": [0, 0, 0]}]},
        {"rebuild": "keep", "libraries": {"footprints": {"L": 5}}, "components": [
            {"ref": "U1", "footprint": "L:X", "at": [0, 0, 0]},
            {"ref": "U2", "footprint": "nocolon", "at": [0, 0, 0]}]},
    ]
    for design in weird:
        errors, warnings = _check(design, _ctx(tmp_path))
        assert all(isinstance(e, str) for e in errors + warnings)


# --- IM-E1: keep rebuild, unplaced schematic parts --------------------------------------------
def test_e1_keep_part_without_placement_is_an_error(tmp_path: Path, imported: dict) -> None:
    imported["components"][1].pop("at")
    imported["components"].append(_comp("R3"))
    errors, warnings = _check(imported, _ctx(tmp_path))
    assert _ids(errors) == ["IM-E1"] and warnings == []
    assert "R2, R3" in errors[0] and "2 part(s)" in errors[0]


def test_e1_x_y_or_board_only_count_as_placed(tmp_path: Path, imported: dict) -> None:
    imported["components"][1] = _comp("R2", x=3.0, y=2.0)
    imported["components"][2] = _hole("H1")                      # board-only, no `at`
    assert _check(imported, _ctx(tmp_path)) == ([], [])
    imported["components"][1] = _comp("R2", x=3.0)                # x without y: unplaced
    assert _ids(_check(imported, _ctx(tmp_path))[0]) == ["IM-E1"]
    imported["components"][1] = _comp("R2", at=None)              # null `at`: unplaced
    assert _ids(_check(imported, _ctx(tmp_path))[0]) == ["IM-E1"]


def test_e1_only_applies_to_keep(tmp_path: Path, imported: dict) -> None:
    for comp in imported["components"]:
        comp.pop("at")
    assert _check({**imported, "rebuild": "reflow"}, _ctx(tmp_path)) == ([], [])
    no_mode = {k: v for k, v in imported.items() if k != "rebuild"}
    assert _check(no_mode, _ctx(tmp_path)) == ([], [])


def test_e1_exempts_parts_a_composed_block_added(tmp_path: Path, imported: dict) -> None:
    # design_blocks.compose keeps the base's rebuild 'keep' and strips the block
    # parts' placement: the pipeline places them, the source parts stay fixed
    block_parts = [_comp("U5", rot=90.0), _comp("C15")]
    blocks = [{"name": "GNSS", "ref_map": {"U1": "U5", "C1": "C15"}},
              {"name": "junk", "ref_map": ["not a map"]}, "not a block"]
    design = {**imported, "components": imported["components"] + block_parts,
              "blocks": blocks}
    assert _check(design, _ctx(tmp_path)) == ([], [])
    design["components"][1] = _comp("R2")                         # a source part: still E1
    errors, _ = _check(design, _ctx(tmp_path))
    assert _ids(errors) == ["IM-E1"] and ": R2 (" in errors[0] and "U5" not in errors[0]
    assert _ids(_check({**design, "blocks": "x"}, _ctx(tmp_path))[0]) == ["IM-E1"]


def test_e1_lists_at_most_a_window_of_refs(tmp_path: Path, imported: dict) -> None:
    extra = [_comp(f"C{i}") for i in range(1, 31)]
    imported["components"] = imported["components"] + extra
    errors, _ = _check(imported, _ctx(tmp_path))
    assert "30 part(s)" in errors[0] and "C1, C2" in errors[0]
    assert f"(+{30 - im.MAX_LISTED} more)" in errors[0] and "C30" not in errors[0]


# --- IM-E2: source.reference ------------------------------------------------------------------
def test_e2_a_valid_reference_passes(tmp_path: Path, imported: dict) -> None:
    for uri in ("reference_padnets.json", "${KIPRJMOD}/reference_padnets.json",
                "$(KIPRJMOD)/reference_padnets.json",
                str(tmp_path / "reference_padnets.json")):
        design = {**imported, "source": {**imported["source"], "reference": uri}}
        assert _check(design, _ctx(tmp_path)) == ([], []), uri


def test_e2_a_missing_reference_is_an_error(tmp_path: Path, imported: dict) -> None:
    design = {**imported, "source": {**imported["source"], "reference": "gone.json"}}
    errors, _ = _check(design, _ctx(tmp_path))
    assert _ids(errors) == ["IM-E2"] and "gone.json" in errors[0] and "missing" in errors[0]


@pytest.mark.parametrize("content, why", [
    ("{not json", "unreadable"),
    ('{"format": "kicad-eda.padnets/2", "nets": {}}', "kicad-eda.padnets/1"),
    ('["kicad-eda.padnets/1"]', "kicad-eda.padnets/1"),
    ('{"format": "kicad-eda.padnets/1", "nets": []}', "unreadable"),
])
def test_e2_an_unreadable_or_foreign_reference_is_an_error(tmp_path: Path, imported: dict,
                                                           content: str, why: str) -> None:
    (tmp_path / "reference_padnets.json").write_text(content, encoding="utf-8")
    errors, _ = _check(imported, _ctx(tmp_path))
    assert _ids(errors) == ["IM-E2"] and why in errors[0]


def test_e2_a_directory_or_non_utf8_reference_is_an_error(tmp_path: Path,
                                                          imported: dict) -> None:
    (tmp_path / "refdir").mkdir()
    design = {**imported, "source": {**imported["source"], "reference": "refdir"}}
    assert _ids(_check(design, _ctx(tmp_path))[0]) == ["IM-E2"]
    (tmp_path / "reference_padnets.json").write_bytes(b"\xff\xfe\x00bad")
    assert _ids(_check(imported, _ctx(tmp_path))[0]) == ["IM-E2"]


@pytest.mark.parametrize("value", [5, ["a.json"], "${OTHER_VAR}/ref.json", "   "])
def test_e2_a_reference_that_is_no_usable_path_is_an_error(tmp_path: Path, imported: dict,
                                                           value: Any) -> None:
    design = {**imported, "source": {**imported["source"], "reference": value}}
    errors, _ = _check(design, _ctx(tmp_path))
    assert _ids(errors) == ["IM-E2"]


def test_e2_unset_or_unjudgeable_reference_is_skipped(tmp_path: Path, imported: dict) -> None:
    for value in (None, ""):
        design = {**imported, "source": {**imported["source"], "reference": value}}
        assert _check(design, _ctx(tmp_path)) == ([], [])
    # a relative path without a netlist directory cannot be located: no finding
    design = {**imported, "source": {**imported["source"], "reference": "gone.json"}}
    errors, _ = _check(design, _ctx(None))
    assert "IM-E2" not in _ids(errors)
    absolute = {**imported, "source": {**imported["source"],
                                       "reference": str(tmp_path / "gone.json")}}
    assert "IM-E2" in _ids(_check(absolute, _ctx(None))[0])


# --- IM-E3: variant.dropped_refs still present ----------------------------------------------
def test_e3_a_dropped_ref_still_present_is_an_error(tmp_path: Path, imported: dict) -> None:
    design = {**imported, "variant": {"mode": "keep", "dropped_refs": ["R2", "H1", "LOGO9"]}}
    errors, _ = _check(design, _ctx(tmp_path))
    assert _ids(errors) == ["IM-E3", "IM-E3"]
    assert "R2" in errors[0] and "components" in errors[0] and "'N1', 'N2'" in errors[0]
    assert "H1" in errors[1] and "components" in errors[1] and "nets" not in errors[1]


def test_e3_a_dropped_ref_only_in_a_net_is_an_error(tmp_path: Path, imported: dict) -> None:
    imported["nets"]["N3"] = [["LOGO1", "1"], ["R1", "1"]]
    design = {**imported, "variant": {"mode": "reflow", "dropped_refs": ["LOGO1"]}}
    errors, _ = _check(design, _ctx(tmp_path))
    assert errors == ["IM-E3 variant.dropped_refs names LOGO1, but it is still in nets 'N3'"]


def test_e3_dropped_refs_that_are_gone_pass(tmp_path: Path, imported: dict) -> None:
    design = {**imported, "variant": {"mode": "keep", "dropped_refs": ["LOGO1", "H9"]}}
    assert _check(design, _ctx(tmp_path)) == ([], [])
    no_list = {**imported, "variant": {"mode": "keep"}}
    assert _check(no_list, _ctx(tmp_path)) == ([], [])


# --- IM-E4: flags must be booleans ------------------------------------------------------------
@pytest.mark.parametrize("key, value", [("dnp", "yes"), ("in_bom", 0), ("board_only", None),
                                        ("dnp", 1), ("in_bom", "false")])
def test_e4_a_non_boolean_flag_is_an_error(tmp_path: Path, imported: dict, key: str,
                                           value: Any) -> None:
    imported["components"][0] = {**imported["components"][0], key: value}
    errors, _ = _check(imported, _ctx(tmp_path))
    assert _ids(errors) == ["IM-E4"]
    assert errors[0].startswith(f"IM-E4 R1: '{key}' must be true or false")
    assert json.dumps(value) in errors[0]


def test_e4_boolean_flags_pass(tmp_path: Path, imported: dict) -> None:
    imported["components"][0] = {**imported["components"][0], "dnp": True, "in_bom": False}
    imported["components"][1] = {**imported["components"][1], "board_only": False}
    assert _check(imported, _ctx(tmp_path)) == ([], [])


# --- IM-W1: source.path and sha256 -------------------------------------------------------------
def test_w1_a_missing_source_is_a_warning(tmp_path: Path, imported: dict) -> None:
    design = {**imported, "source": {**imported["source"], "path": "orig/gone.kicad_pro"}}
    errors, warnings = _check(design, _ctx(tmp_path))
    assert errors == [] and _ids(warnings) == ["IM-W1"]
    assert "gone.kicad_pro" in warnings[0] and "missing" in warnings[0]


def test_w1_a_changed_source_is_a_warning(tmp_path: Path, imported: dict) -> None:
    (tmp_path / "orig" / "board.kicad_pro").write_text("edited", encoding="utf-8")
    errors, warnings = _check(imported, _ctx(tmp_path))
    assert errors == [] and _ids(warnings) == ["IM-W1"]
    assert "source changed since import" in warnings[0]


def test_w1_matching_or_unhashed_source_passes(tmp_path: Path, imported: dict) -> None:
    upper = {**imported, "source": {**imported["source"],
                                    "sha256": imported["source"]["sha256"].upper()}}
    assert _check(upper, _ctx(tmp_path)) == ([], [])
    unhashed = {**imported, "source": {k: v for k, v in imported["source"].items()
                                       if k != "sha256"}}
    assert _check(unhashed, _ctx(tmp_path)) == ([], [])
    bad_sha = {**imported, "source": {**imported["source"], "sha256": 42}}
    assert _check(bad_sha, _ctx(tmp_path)) == ([], [])


def test_w1_a_source_that_is_no_file_is_a_warning(tmp_path: Path, imported: dict) -> None:
    for path in ("orig", 7, "${OTHER}/x.kicad_pro"):
        design = {**imported, "source": {**imported["source"], "path": path}}
        errors, warnings = _check(design, _ctx(tmp_path))
        assert errors == [] and _ids(warnings) == ["IM-W1"], path


def test_w1_unset_or_unjudgeable_source_path_is_skipped(tmp_path: Path, imported: dict) -> None:
    no_path = {**imported, "source": {k: v for k, v in imported["source"].items()
                                      if k != "path"}}
    assert _check(no_path, _ctx(tmp_path)) == ([], [])
    # relative path, no netlist directory: no W1 (and the relative reference is skipped too)
    assert _check(imported, _ctx(None)) == ([], [])


def test_w1_an_unreadable_source_is_a_warning(tmp_path: Path, imported: dict,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_path: Path) -> str:
        raise PermissionError("denied")

    monkeypatch.setattr(im, "_sha256_of", boom)
    errors, warnings = _check(imported, _ctx(tmp_path))
    assert errors == [] and _ids(warnings) == ["IM-W1"] and "denied" in warnings[0]


def test_sha256_of_streams_the_file(tmp_path: Path) -> None:
    blob = tmp_path / "big.bin"
    blob.write_bytes(b"x" * (im.HASH_CHUNK * 2 + 17))
    assert im._sha256_of(blob) == hashlib.sha256(blob.read_bytes()).hexdigest()


# --- IM-W2: reflow with mostly fixed parts ------------------------------------------------------
def test_w2_reflow_with_most_parts_fixed_is_a_warning(tmp_path: Path, imported: dict) -> None:
    design = {**imported, "rebuild": "reflow"}            # 3 of 3 parts carry `at`
    errors, warnings = _check(design, _ctx(tmp_path))
    assert errors == [] and _ids(warnings) == ["IM-W2"] and "3 of 3" in warnings[0]


def test_w2_reflow_with_half_or_fewer_fixed_passes(tmp_path: Path, imported: dict) -> None:
    comps = [_comp("R1", at=[1, 1, 0]), _comp("R2"), _comp("R3", at=[2, 2, 0]), _comp("R4")]
    design = {**imported, "rebuild": "reflow", "components": comps,
              "nets": {"N1": [["R1", "1"], ["R2", "1"], ["R3", "1"], ["R4", "1"]]}}
    assert _check(design, _ctx(tmp_path)) == ([], [])
    assert _check({**design, "components": []}, _ctx(tmp_path)) == ([], [])


# --- IM-W3: footprints newer than the D0 dialect ----------------------------------------------
def _footprint(pretty: Path, name: str, version: int | None) -> Path:
    pretty.mkdir(parents=True, exist_ok=True)
    head = (f'(footprint "{name}" (version {version}) (generator "pcbnew")'
            if version is not None else f"(module {name} (layer F.Cu)")
    path = pretty / f"{name}.kicad_mod"
    path.write_text(head + '\n  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))\n)\n',
                    encoding="utf-8")
    return path


def _with_library(design: dict, uri: str, names: list[str]) -> dict:
    comps = [_comp(f"U{i}", footprint=f"Imp:{n}", at=[i, i, 0]) for i, n in enumerate(names, 1)]
    return {**design, "libraries": {"footprints": {"Imp": uri}},
            "components": design["components"] + comps}


def test_w3_a_too_new_footprint_is_a_warning(tmp_path: Path, imported: dict) -> None:
    pretty = tmp_path / "libs" / "Imp.pretty"
    _footprint(pretty, "New", K10)
    _footprint(pretty, "Newer", K11)
    _footprint(pretty, "Old", K9)
    _footprint(pretty, "Legacy", None)
    design = _with_library(imported, "libs/Imp.pretty", ["New", "Old", "Legacy", "Newer", "New"])
    errors, warnings = _check(design, _ctx(tmp_path))
    assert errors == [] and _ids(warnings) == ["IM-W3"]
    w = warnings[0]
    assert "Imp" in w and f"New.kicad_mod (version {K10})" in w
    assert f"Newer.kicad_mod (version {K11})" in w and "Old.kicad_mod" not in w
    assert "2 of 4" in w and f"> {K9}" in w                      # Legacy has no version
    assert "KiCad 9 pcbnew cannot load it" in w and "kicad_dialect" in w


def test_w3_d0_footprints_pass(tmp_path: Path, imported: dict) -> None:
    pretty = tmp_path / "libs" / "Imp.pretty"
    _footprint(pretty, "Old", K9)
    for uri in ("libs/Imp.pretty", "${KIPRJMOD}/libs/Imp.pretty", str(pretty)):
        design = _with_library(imported, uri, ["Old", "Missing"])
        assert _check(design, _ctx(tmp_path)) == ([], []), uri


def test_w3_uses_the_probed_swig_limit(tmp_path: Path, imported: dict) -> None:
    pretty = tmp_path / "libs" / "Imp.pretty"
    _footprint(pretty, "New", K10)
    _footprint(pretty, "Newer", K11)
    design = _with_library(imported, "libs/Imp.pretty", ["New"])
    assert _check(design, _ctx(tmp_path, swig_fp_version_max=K10)) == ([], [])
    design = _with_library(imported, "libs/Imp.pretty", ["New", "Newer"])
    _, warnings = _check(design, _ctx(tmp_path, swig_fp_version_max=K10))
    assert _ids(warnings) == ["IM-W3"] and f"> {K10}" in warnings[0]
    assert "New.kicad_mod" not in warnings[0].replace("Newer.kicad_mod", "")
    assert "KiCad 9" not in warnings[0] and f"up to (version {K10})" in warnings[0]


def test_w3_samples_at_most_50_files(tmp_path: Path, imported: dict) -> None:
    pretty = tmp_path / "libs" / "Imp.pretty"
    names = [f"P{i:03d}" for i in range(60)]
    for i, name in enumerate(names):
        _footprint(pretty, name, K9 if i < im.MAX_SAMPLED else K11)
    design = _with_library(imported, "libs/Imp.pretty", names)
    assert _check(design, _ctx(tmp_path)) == ([], [])          # files 51..60 never read
    _footprint(pretty, names[0], K11)
    _, warnings = _check(design, _ctx(tmp_path))
    assert _ids(warnings) == ["IM-W3"] and "1 of 50" in warnings[0]


def test_w3_lists_at_most_a_window_of_files(tmp_path: Path, imported: dict) -> None:
    pretty = tmp_path / "libs" / "Imp.pretty"
    names = [f"Q{i:02d}" for i in range(im.MAX_LISTED + 3)]
    for name in names:
        _footprint(pretty, name, K11)
    _, warnings = _check(_with_library(imported, "libs/Imp.pretty", names), _ctx(tmp_path))
    assert "(+3 more)" in warnings[0] and names[-1] not in warnings[0]


def test_w3_skips_what_it_cannot_locate(tmp_path: Path, imported: dict) -> None:
    pretty = tmp_path / "libs" / "Imp.pretty"
    _footprint(pretty, "New", K11)
    relative = _with_library(imported, "libs/Imp.pretty", ["New"])
    assert "IM-W3" not in _ids(_check(relative, _ctx(None))[1])      # no netlist dir
    other_var = _with_library(imported, "${OTHER}/Imp.pretty", ["New"])
    assert _check(other_var, _ctx(tmp_path)) == ([], [])
    stock = {**imported, "libraries": {"footprints": {"Imp": "libs/Imp.pretty"}},
             "components": imported["components"] + [_comp("U1", footprint="Stock:New",
                                                           at=[0, 0, 0])]}
    assert _check(stock, _ctx(tmp_path)) == ([], [])       # not through the block
    absolute = _with_library(imported, str(pretty), ["New"])
    assert _ids(_check(absolute, _ctx(None))[1]) == ["IM-W3"]


# --- IM-W4: board-only parts in no_connect ------------------------------------------------------
def test_w4_a_board_only_part_in_no_connect_is_a_warning(tmp_path: Path, imported: dict) -> None:
    design = {**imported, "no_connect": [["H1", "1"], ["R9", "1"], ["H1", 2]]}
    errors, warnings = _check(design, _ctx(tmp_path))
    assert _ids(warnings) == ["IM-W4"] and "H1" in warnings[0] and "H1.1, H1.2" in warnings[0]
    assert "IM-E" not in " ".join(errors)


def test_w4_schematic_parts_in_no_connect_pass(tmp_path: Path, imported: dict) -> None:
    imported["nets"] = {"N1": [["R1", "1"], ["R2", "1"]]}
    design = {**imported, "no_connect": [["R1", "2"], ["R2", "2"]]}
    assert _check(design, _ctx(tmp_path)) == ([], [])


# --- order, registry, lint_netlist --------------------------------------------------------------
def test_findings_come_in_rule_order(tmp_path: Path, imported: dict) -> None:
    imported["components"][0] = {**imported["components"][0], "dnp": "y"}
    imported["components"][1].pop("at")
    (tmp_path / "orig" / "board.kicad_pro").write_text("edited", encoding="utf-8")
    pretty = tmp_path / "libs" / "Imp.pretty"
    _footprint(pretty, "New", K11)
    design = _with_library(imported, "libs/Imp.pretty", ["New"])
    design = {**design, "source": {**design["source"], "reference": "gone.json"},
              "variant": {"mode": "keep", "dropped_refs": ["R1"]},
              "no_connect": [["H1", "1"]]}
    errors, warnings = _check(design, _ctx(tmp_path))
    assert _ids(errors) == ["IM-E1", "IM-E2", "IM-E3", "IM-E4"]
    assert _ids(warnings) == ["IM-W1", "IM-W3", "IM-W4"]
    reflow = {**design, "rebuild": "reflow"}
    assert _ids(_check(reflow, _ctx(tmp_path))[1]) == ["IM-W1", "IM-W2", "IM-W3", "IM-W4"]


def test_lint_netlist_discovers_the_module_at_order_40() -> None:
    rules = {r.name: r for r in lint_netlist.rule_modules()}
    rule = rules["lint_rules_import"]
    assert rule.order == im.ORDER == 40 and rule.problem == "" and rule.check is im.check


def test_lint_netlist_reports_the_import_rules(tmp_path: Path, imported: dict) -> None:
    imported["components"][1].pop("at")
    rep = lint_netlist.lint(imported, ctx=_ctx(tmp_path))
    ours = [e for e in rep["errors"] if e.startswith("IM-") or "lint_rules_import" in e]
    assert not rep["ok"] and ours == im.check(imported, _ctx(tmp_path))[0]
    assert len(ours) == 1 and ours[0].startswith("IM-E1") and "R2" in ours[0]
    assert not [w for w in rep["warnings"] if w.startswith("IM-")]


def test_lint_netlist_cli_passes_the_netlist_dir(tmp_path: Path, imported: dict,
                                                 capsys: pytest.CaptureFixture) -> None:
    design = {**imported, "source": {**imported["source"], "reference": "gone.json"}}
    path = tmp_path / "netlist.json"
    path.write_text(json.dumps(design), encoding="utf-8")
    assert lint_netlist.main([str(path)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert any(e.startswith("IM-E2") for e in out["errors"])


# --- module constraints -------------------------------------------------------------------------
def _import_time_nodes(node: ast.AST) -> list[ast.AST]:
    """The nodes that run when the module is imported: function bodies (where
    e.g. sexpr_read imports sexpdata lazily) are not descended into."""
    out: list[ast.AST] = []
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            out += [child, *_import_time_nodes(child)]
    return out


def _imports(path: Path) -> set[str]:
    nodes = _import_time_nodes(ast.parse(path.read_text(encoding="utf-8"),
                                         feature_version=(3, 11)))
    names = {a.name.split(".")[0] for n in nodes if isinstance(n, ast.Import) for a in n.names}
    return names | {n.module.split(".")[0] for n in nodes
                    if isinstance(n, ast.ImportFrom) and n.module and n.level == 0}


def test_module_is_stdlib_only_transitively() -> None:
    """Every import-time import, followed through the server/tools siblings,
    is stdlib (the proof that it runs on stdlib alone is the next test)."""
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    seen: set[str] = set()
    todo = ["lint_rules_import"]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        for dep in _imports(TOOLS / f"{name}.py"):
            if (TOOLS / f"{dep}.py").is_file():
                todo.append(dep)
            else:
                assert dep in stdlib, f"{name} imports non-stdlib {dep!r}"
    assert "pcbnew" not in seen and {"lint_context", "fp_dialect"} <= seen


_STDLIB_RUN = """
import json, sys
sys.path.insert(0, sys.argv[1])
import lint_rules_import as im
from lint_context import LintContext
ctx = LintContext(netlist_dir=sys.argv[2])
found = [im.check(json.loads(open(p, encoding="utf-8").read()), ctx) for p in sys.argv[3:]]
tools = {p.rsplit(".", 1)[0] for p in __import__("os").listdir(sys.argv[1])} | {"__main__"}
foreign = sorted(m.split(".")[0] for m in sys.modules
                 if m.split(".")[0] not in sys.stdlib_module_names and m.split(".")[0] not in tools)
print(json.dumps({"found": found, "foreign": foreign}))
"""


def test_every_rule_runs_on_the_stdlib_alone(tmp_path: Path, imported: dict) -> None:
    """python -I -S: no site-packages at all, only server/tools on the path."""
    imported["components"][0] = {**imported["components"][0], "dnp": "y"}
    imported["components"][1].pop("at")
    (tmp_path / "orig" / "board.kicad_pro").write_text("edited", encoding="utf-8")
    _footprint(tmp_path / "libs" / "Imp.pretty", "New", K11)
    keep = {**_with_library(imported, "libs/Imp.pretty", ["New"]),
            "variant": {"mode": "keep", "dropped_refs": ["R1"]}, "no_connect": [["H1", "1"]]}
    keep["source"] = {**keep["source"], "reference": "gone.json"}
    paths = []
    for name, design in (("keep", keep), ("reflow", {**keep, "rebuild": "reflow"})):
        paths.append(tmp_path / f"{name}.json")
        paths[-1].write_text(json.dumps(design), encoding="utf-8")
    proc = subprocess.run([sys.executable, "-I", "-S", "-c", _STDLIB_RUN, str(TOOLS),
                           str(tmp_path), *map(str, paths)], stdin=subprocess.DEVNULL,
                          capture_output=True, encoding="utf-8", errors="replace", timeout=120)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["foreign"] == []
    (k_err, k_warn), (r_err, r_warn) = out["found"]
    assert _ids(k_err) == ["IM-E1", "IM-E2", "IM-E3", "IM-E4"]
    assert _ids(k_warn) == ["IM-W1", "IM-W3", "IM-W4"]
    assert _ids(r_warn) == ["IM-W1", "IM-W2", "IM-W3", "IM-W4"] and "IM-E1" not in _ids(r_err)


def test_module_size_functions_and_docstring() -> None:
    src = MODULE.read_text(encoding="utf-8")
    assert len(src.splitlines()) < 800
    tree = ast.parse(src, feature_version=(3, 11))
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert funcs and all(n.end_lineno - n.lineno < 50 for n in funcs)
    assert all(n.returns is not None for n in funcs)
    doc = ast.get_docstring(tree) or ""
    for rule in ("IM-E1", "IM-E2", "IM-E3", "IM-E4", "IM-W1", "IM-W2", "IM-W3", "IM-W4"):
        assert rule in doc, rule
