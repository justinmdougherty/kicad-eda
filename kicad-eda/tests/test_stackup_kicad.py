"""stackup_kicad in the dev venv (ADR D7.1 'Rules for the block', D7.3, D7.5).

The golden references for ``emit`` are KiCad's own saved blocks: the D7 splice
probes saved by KiCad 9.0.4 and 10.0.5 (tests/fixtures/stackup_kicad/kicad_saved,
copied from the planning scratch). Emit snapshots live in
tests/fixtures/stackup_kicad/emit (regenerate with
KICAD_EDA_REGEN_STACKUP_FIXTURES=1). The D7-02 stackup fixtures
(tests/fixtures/stackups) drive the round trips.
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

import pytest

import stackup_kicad as sk
import stackup_model as sm
from stackup_roles import kicad_layer_id

FIXTURES = Path(__file__).resolve().parent / "fixtures"
STACKUPS = FIXTURES / "stackups"
SNAPSHOTS = FIXTURES / "stackup_kicad" / "emit"
SAVED = FIXTURES / "stackup_kicad" / "kicad_saved"
REGEN = os.environ.get("KICAD_EDA_REGEN_STACKUP_FIXTURES") == "1"


def _fixture(name: str) -> dict:
    return json.loads((STACKUPS / f"{name}.json").read_text(encoding="utf-8"))


def _rs(name: str) -> sm.ResolvedStackup:
    rs, issues = sm.resolve(_fixture(name)["design"])
    assert rs is not None, issues
    return rs


ALL = sorted(p.stem for p in STACKUPS.glob("*.json"))
RESOLVED = [n for n in ALL if sm.resolve(_fixture(n)["design"])[0] is not None]
MODEL = [n for n in RESOLVED if _rs(n).legacy is None]
SNAPSHOT_NAMES = ("d722_2l_mixed", "d722_4l_hybrid", "d722_6l_2116", "default_8l",
                  "s6l_hybrid", "single_sided")

HEAD = """(kicad_pcb
\t(version 20241229)
\t(generator "pcbnew")
\t(generator_version "9.0")
\t(general
\t\t(thickness {thickness})
\t\t(legacy_teardrops no)
\t)
\t(paper "A4")
\t(layers
{layers}
\t\t(9 "F.Adhes" user "F.Adhesive")
\t\t(1 "F.Mask" user)
\t\t(25 "Edge.Cuts" user)
\t)
\t(setup
\t\t(pad_to_mask_clearance 0)
\t\t(pcbplotparams
\t\t\t(outputdirectory "gerbers (v1)/")
\t\t)
\t)
\t(net 0 "")
\t(net 1 "GND")
\t(footprint "X:Y"
\t\t(property "Value" "(stackup (layer \\"F.Cu\\")) trap")
\t)
)
"""


def _board_text(rs: sm.ResolvedStackup, *, block: bool = True, copper: tuple | None = None,
                types: dict | None = None, thickness: float | None = None) -> str:
    """A KiCad 9-shaped board head for `rs` (layers with types, general thickness),
    with the emitted stackup spliced in unless block=False."""
    names = copper if copper is not None else rs.copper_names()
    kinds = {c.name: c.kicad_type for c in rs.copper} | dict(types or {})
    layers = "\n".join(f'\t\t({kicad_layer_id(n)} "{n}" {kinds.get(n, "signal")})' for n in names)
    text = HEAD.format(layers=layers, thickness=sk.kicad_mm(
        rs.board_thickness_computed if thickness is None else thickness))
    return sk.splice(text, sk.emit(rs)) if block else text


def _tokens(text: str) -> list[str]:
    return re.findall(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()"]+', text)


def _with_stackup(name: str, **changes: object) -> dict:
    design = copy.deepcopy(_fixture(name)["design"])
    design["stackup"] = {**design["stackup"], **changes}
    return design


# ---------------------------------------------------------------------------
# emit: KiCad's own saved blocks, snapshots, the D7.1 rules
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("saved", ["s6l_hybrid_k9.txt", "s6l_hybrid_k10.txt"])
def test_emit_is_byte_identical_to_the_block_kicad_saved(saved):
    """The s6L_hybrid splice probe as KiCad 9.0.4 / 10.0.5 re-saved it (D7.1 [K10])."""
    kicad = (SAVED / saved).read_text(encoding="utf-8")
    block = sk.emit(_rs("s6l_hybrid"))
    assert block == kicad.strip("\n").lstrip("\t")
    assert sk.normalize(block) == sk.normalize(kicad)


@pytest.mark.parametrize("name", SNAPSHOT_NAMES)
def test_emit_snapshots(name):
    path = SNAPSHOTS / f"{name}.txt"
    block = sk.emit(_rs(name))
    if REGEN:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(block + "\n", encoding="utf-8", newline="\n")
    assert block + "\n" == path.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", MODEL)
def test_emit_follows_the_block_rules(name):
    rs = _rs(name)
    text = sk.emit(rs)
    block = sk.parse_block(text)
    names = [i.name for i in block.items]
    assert names[:3] == ["F.SilkS", "F.Paste", "F.Mask"]
    assert names[-3:] == ["B.Mask", "B.Paste", "B.SilkS"]
    assert [c.name for c in block.copper()] == list(rs.copper_names())
    assert [c.sublayers[0].thickness for c in block.copper()] == [c.thickness for c in rs.copper]
    dies = block.dielectrics()
    assert [d.name for d in dies] == [f"dielectric {k}" for k in range(1, len(rs.dielectrics) + 1)]
    for got, want in zip(dies, rs.dielectrics, strict=True):
        assert got.type == want.kind
        assert [s.thickness for s in got.sublayers] == [p.thickness for p in want.plies]
        assert [s.locked for s in got.sublayers] == [want.locked] * len(want.plies)
    plies = sum(len(d.plies) for d in rs.dielectrics)
    assert len(re.findall(r"\) addsublayer\n", text)) == plies - len(rs.dielectrics)
    assert "(addsublayer" not in text
    for side in ("F.Mask", "B.Mask"):
        assert block.item(side).sublayers[0].thickness == rs.mask.over_substrate
    assert block.dielectric_constraints is rs.impedance_controlled
    assert block.thickness() == pytest.approx(rs.board_thickness_computed, abs=1e-9)


def test_emit_is_deterministic_and_reads_rs_only():
    rs = _rs("d722_4l_hybrid")
    before = rs.to_dict()
    assert sk.emit(rs) == sk.emit(rs) == sk.emit(rs, dialect=10)
    assert rs.to_dict() == before


def test_emit_refuses_legacy_and_unknown_dialects():
    with pytest.raises(ValueError, match="legacy"):
        sk.emit(_rs("legacy"))
    with pytest.raises(ValueError, match="dialect"):
        sk.emit(_rs("d722_2l_mixed"), dialect=11)


def test_emit_without_a_mask_keeps_the_mask_layers_at_zero_thickness():
    rs, issues = sm.resolve(_with_stackup("d722_2l_mixed", mask=None))
    assert rs is not None and rs.mask.source == "none", issues
    block = sk.parse_block(sk.emit(rs))
    for side in ("F.Mask", "B.Mask"):
        sub = block.item(side).sublayers[0]
        assert sub.thickness == 0 and sub.material is None and sub.epsilon_r is None
    assert block.thickness() == pytest.approx(1.58, abs=1e-9) == rs.board_thickness_computed


def test_emit_mask_material_epsilon_and_color():
    block = sk.parse_block(sk.emit(_rs("s6l_hybrid")))
    mask = block.item("F.Mask").sublayers[0]
    assert (mask.color, mask.thickness, mask.material, mask.epsilon_r, mask.loss_tangent) == (
        "Green", 0.01, "LPI", 3.8, 0.025)
    fab_mask = sk.parse_block(sk.emit(_rs("d722_6l_2116"))).item("B.Mask").sublayers[0]
    assert (fab_mask.color, fab_mask.thickness, fab_mask.material) == (None, 0.0305, "LPI")


@pytest.mark.parametrize(("finish", "kicad"), [
    ("ENIG", "ENIG"), ("HASL", "HAL SnPb"), ("LF-HASL", "HAL lead-free"), ("OSP", "OSP"),
    ("ImAg", "Immersion silver"), ("ImSn", "Immersion tin"), (None, "None")])
def test_copper_finish_uses_kicads_names(finish, kicad):
    design = _with_stackup("d722_2l_mixed", finish=finish)
    if finish is None:
        del design["stackup"]["finish"]
    rs, _ = sm.resolve(design)
    assert sk.parse_block(sk.emit(rs)).copper_finish == kicad
    back = sk.netlist_stackup_from_board(sk.emit(rs))
    assert back.get("finish") == finish


def test_ply_labels_carry_the_construction():
    labels = [s.material for d in sk.parse_block(sk.emit(_rs("d722_6l_2116"))).dielectrics()
              for s in d.sublayers]
    assert labels[:3] == ["NP-155F 2116", "S1000-2M 2313", "NP-155F"]
    hybrid = [s.material for d in sk.parse_block(sk.emit(_rs("d722_4l_hybrid"))).dielectrics()
              for s in d.sublayers]
    assert hybrid == ["RO4350B", "RO4450F", "370HR 2116", "370HR"]
    design = _with_stackup("d722_2l_mixed", layers=[
        {"type": "copper"}, {"type": "dielectric", "thickness": 1.51, "dk": 4.4},
        {"type": "copper"}])
    rs, _ = sm.resolve(design)
    assert sk.parse_block(sk.emit(rs)).dielectrics()[0].sublayers[0].material == \
        sk.UNNAMED_DIELECTRIC


def test_kicad_number_formats():
    assert sk.kicad_mm(0.03048) == "0.03048"
    assert sk.kicad_mm(1.3075 + 2 * 0.03048) == "1.36846"
    assert sk.kicad_mm(0.0000304) == "0.00003"
    assert sk.kicad_mm(0) == "0"
    assert sk.kicad_mm(1.6) == "1.6"
    assert sk.kicad_er(4.084804) == "4.0848" and sk.kicad_er(3.66) == "3.66"
    assert sk.kicad_double(0.019311) == "0.019311" and sk.kicad_double(0.00005) == "0.00005"
    assert sk.kicad_double(0.02) == "0.02"


# ---------------------------------------------------------------------------
# splice / extract
# ---------------------------------------------------------------------------
def test_splice_inserts_the_block_as_the_first_child_of_setup():
    rs = _rs("d722_6l_2116")
    head = _board_text(rs, block=False)
    block = sk.emit(rs)
    out = sk.splice(head, block)
    assert re.search(r"\(setup\n\t\t\(stackup\n", out)
    assert sk.extract(out) == block
    start = out.index("(stackup")
    assert out[:start] == head[:head.index("(setup") + len("(setup")] + "\n\t\t"
    assert out[start + len(block):] == head[head.index("(setup") + len("(setup"):]


def test_splice_is_idempotent_and_replaces_an_existing_block():
    head = _board_text(_rs("d722_2l_mixed"), block=False)
    first = sk.splice(head, sk.emit(_rs("d722_2l_mixed")))
    assert sk.splice(first, sk.emit(_rs("d722_2l_mixed"))) == first
    other = sk.emit(_rs("default_8l"))
    replaced = sk.splice(first, other)
    assert sk.extract(replaced) == other
    assert len(re.findall(r"^\t\t\(stackup$", replaced, re.M)) == 1


def test_quoted_parentheses_never_confuse_the_scanner():
    head = _board_text(_rs("d722_2l_mixed"), block=False)
    assert sk.extract(head) is None              # "(stackup ..." inside a quoted value
    out = sk.splice(head, sk.emit(_rs("d722_2l_mixed")))
    assert '"(stackup (layer \\"F.Cu\\")) trap"' in out
    assert '(outputdirectory "gerbers (v1)/")' in out


def test_splice_and_extract_refuse_broken_input():
    with pytest.raises(ValueError, match="setup"):
        sk.splice("(kicad_pcb (version 20241229) (general (thickness 1.6)))",
                  sk.emit(_rs("d722_2l_mixed")))
    with pytest.raises(ValueError, match="unbalanced"):
        sk.extract("(kicad_pcb (setup (stackup (layer \"F.Cu\")) ")
    with pytest.raises(ValueError, match="stackup"):
        sk.splice(_board_text(_rs("d722_2l_mixed"), block=False), "(setup)")


# ---------------------------------------------------------------------------
# parse_block and the board readers
# ---------------------------------------------------------------------------
def test_parse_block_reads_kicads_saved_values():
    block = sk.parse_block((SAVED / "s2l_k9.txt").read_text(encoding="utf-8"))
    assert block.item("F.SilkS").sublayers[0].color == "White"
    assert block.copper_finish == "HAL lead-free" and block.dielectric_constraints is False
    core = block.dielectrics()[0].sublayers[0]
    assert (core.thickness, core.material, core.epsilon_r, core.loss_tangent) == (
        1.51, "FR4", 4.6, 0.02)
    h4 = sk.parse_block((SAVED / "h4_k9.txt").read_text(encoding="utf-8"))
    d1, d2, _ = h4.dielectrics()
    assert d1.sublayers[0].locked and d1.type == "core"
    assert [(s.thickness, s.material, s.epsilon_r) for s in d2.sublayers] == [
        (0.101, "RO4450F", 3.52), (0.12, "370HR 2116", 4.04)]
    assert h4.thickness() == pytest.approx(1.3685, abs=1e-9)


def test_parse_block_takes_a_board_and_needs_a_block():
    rs = _rs("default_8l")
    board = _board_text(rs)
    assert sk.parse_block(board) == sk.parse_block(sk.emit(rs))
    with pytest.raises(ValueError, match=r"no \(stackup"):
        sk.parse_block(_board_text(rs, block=False))


def test_board_layer_readers():
    rs = _rs("d722_6l_2116")
    text = _board_text(rs, thickness=1.6554)
    assert sk.enabled_copper(text) == rs.copper_names()
    assert sk.copper_types(text) == {c.name: c.kicad_type for c in rs.copper}
    assert sk.general_thickness(text) == 1.6554
    assert ("In1.Cu", "power") in [(d.name, d.type) for d in sk.board_layers(text)]


# ---------------------------------------------------------------------------
# compare (ADR D7.5 step 5)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", RESOLVED)
def test_compare_accepts_a_faithful_board(name):
    rs = _rs(name)
    assert sk.compare(rs, _board_text(rs, block=rs.legacy is None)) == []


def test_compare_names_every_mismatch():
    rs = _rs("d722_4l_hybrid")
    assert any("copper" in p for p in sk.compare(rs, _board_text(
        rs, copper=("F.Cu", "B.Cu"))))
    types = sk.compare(rs, _board_text(rs, types={"In1.Cu": "signal"}))
    assert len(types) == 1 and "In1.Cu" in types[0] and "power" in types[0]
    thick = sk.compare(rs, _board_text(rs, thickness=rs.board_thickness_computed + 0.002))
    assert len(thick) == 1 and "thickness" in thick[0]
    good = _board_text(rs)
    edited = good.replace("(epsilon_r 3.66)", "(epsilon_r 3.48)", 1)
    block = sk.compare(rs, edited)
    assert len(block) == 1 and "3.48" in block[0] and "3.66" in block[0]
    missing = sk.compare(rs, _board_text(rs, block=False))
    assert len(missing) == 1 and "no (stackup" in missing[0]


def test_compare_skips_the_block_for_the_legacy_shorthand():
    rs = _rs("legacy")
    assert sk.compare(rs, _board_text(rs, block=False)) == []
    assert sk.compare(rs, _board_text(rs, block=False, thickness=1.6)) != []


# ---------------------------------------------------------------------------
# netlist_stackup_from_board (D7.3 'Stackups from existing designs', F4 import)
# ---------------------------------------------------------------------------
def _physical(rs: sm.ResolvedStackup) -> tuple:
    dies = tuple((d.kind, d.locked, tuple((p.thickness, float(sk.kicad_er(p.er)),
                                           float(sk.kicad_double(p.df))) for p in d.plies))
                 for d in rs.dielectrics)
    mask = rs.mask
    return (rs.copper_layers, rs.kicad_copper_count,
            tuple((c.name, c.thickness) for c in rs.copper), dies, mask.over_substrate,
            None if mask.dk is None else float(sk.kicad_er(mask.dk)), mask.df, mask.color,
            rs.finish, rs.impedance_controlled, rs.board_thickness_computed)


@pytest.mark.parametrize("name", RESOLVED)
def test_netlist_stackup_from_board_round_trips_every_d702_fixture(name):
    design = _fixture(name)["design"]
    rs, issues = sm.resolve(design)
    if rs.legacy is not None:
        with pytest.raises(ValueError, match="legacy"):
            sk.emit(rs)
        return
    block = sk.emit(rs)
    stackup = sk.netlist_stackup_from_board(block)
    assert stackup["origin"] == "designer" and stackup["rationale"] == sk.IMPORT_RATIONALE
    rs2, issues2 = sm.resolve({**design, "stackup": stackup})
    errors = {i.id for i in issues if i.severity == "error"}
    assert rs2 is not None and {i.id for i in issues2 if i.severity == "error"} <= errors, issues2
    assert sk.emit(rs2) == block
    assert _physical(rs2) == _physical(rs)
    assert json.loads(json.dumps(stackup)) == stackup


@pytest.mark.parametrize("name", ["d722_2l_mixed", "d722_6l_2116", "s6l_hybrid", "default_8l"])
def test_roles_and_thickness_come_from_the_board(name):
    design = _fixture(name)["design"]
    rs = _rs(name)
    stackup = sk.netlist_stackup_from_board(_board_text(rs))
    assert stackup["board_thickness"] == rs.board_thickness_computed
    copper = [e for e in stackup["layers"] if e["type"] == "copper"]
    assert [e["role"] for e in copper] == [c.role for c in rs.copper]
    rs2, issues = sm.resolve({**design, "stackup": stackup})
    assert rs2 is not None, issues
    assert [(c.name, c.role, c.net, c.kicad_type) for c in rs2.copper] == \
        [(c.name, c.role, c.net, c.kicad_type) for c in rs.copper]


def test_reader_refuses_a_stackup_that_disagrees_with_the_enabled_copper():
    rs6 = _rs("d722_6l_2116")
    text = _board_text(rs6, copper=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    with pytest.raises(ValueError, match="enables"):
        sk.netlist_stackup_from_board(text)
    with pytest.raises(ValueError, match=r"no \(stackup"):
        sk.netlist_stackup_from_board(_board_text(rs6, block=False))


KICAD11_BLOCK = """(stackup
\t(layer "F.Mask" (type "Top Solder Mask") (thickness 0))
\t(layer "F.Cu" (type "copper") (thickness 0.035))
\t(layer "dielectric 1" (type "core") (thickness 0.508 locked) (material "RO4350B")
\t\t(epsilon_r 3.66) (loss_tangent 0.0037) (spec_frequency 10000000000)
\t\t(dielectric_model djordjevic_sarkar) addsublayer (thickness 0.1)
\t\t(material "FR4") (epsilon_r 4.5) (loss_tangent 0.02) (dielectric_model constant))
\t(layer "B.Cu" (type "copper") (thickness 0.035))
\t(layer "dielectric 2" (type "prepreg") (thickness 0.2))
\t(copper_finish "Hard gold")
\t(dielectric_constraints no))"""


def test_reader_maps_kicad_extras_defaults_and_unknown_finishes():
    with pytest.raises(ValueError, match="alternate"):
        sk.netlist_stackup_from_board(KICAD11_BLOCK)
    text = KICAD11_BLOCK.replace('\t(layer "dielectric 2" (type "prepreg") (thickness 0.2))\n',
                                 "")
    stackup = sk.netlist_stackup_from_board(text)
    core = stackup["layers"][1]
    assert core["locked"] is True and core["kind"] == "core"
    assert core["plies"][0] == {"thickness": 0.508, "material": "RO4350B", "dk": 3.66,
                                "df": 0.0037, "dk_freq_hz": 1e10,
                                "dielectric_model": "djordjevic_sarkar"}
    assert core["plies"][1] == {"thickness": 0.1, "material": "FR4", "dk": 4.5, "df": 0.02}
    assert stackup["mask"] is None and "finish" not in stackup
    assert "impedance_control" not in stackup and "board_thickness" not in stackup
    bare = text.replace('(material "FR4") (epsilon_r 4.5) (loss_tangent 0.02) ', "")
    assert sk.netlist_stackup_from_board(bare)["layers"][1]["plies"][1] == {
        "thickness": 0.1, "material": sk.KICAD_DEFAULT_DIELECTRIC[0], "dk": 4.5, "df": 0.02}
    osp = sk.netlist_stackup_from_board(text.replace('"Hard gold"', '"HT_OSP"'))
    assert osp["finish"] == "OSP"


def test_reader_mask_without_material_keeps_the_thickness_only():
    text = KICAD11_BLOCK.replace('\t(layer "dielectric 2" (type "prepreg") (thickness 0.2))\n',
                                 "").replace("(thickness 0))", '(color "Black") (thickness 0.02))')
    assert sk.netlist_stackup_from_board(text)["mask"] == {"over_substrate": 0.02,
                                                            "color": "Black"}


@pytest.mark.parametrize(("saved", "thickness", "finish"), [
    ("h4_k9.txt", 1.3685, "ENIG"), ("s2l_k9.txt", 1.6, "LF-HASL"),
    ("s6l_hybrid_k10.txt", 1.6284, "ENIG")])
def test_reader_on_blocks_kicad_saved(saved, thickness, finish):
    stackup = sk.netlist_stackup_from_board((SAVED / saved).read_text(encoding="utf-8"))
    design = {**_fixture("d722_2l_mixed")["design"], "stackup": stackup}
    rs, issues = sm.resolve(design)
    assert rs is not None and not [i for i in issues if i.severity == "error"], issues
    assert rs.board_thickness_computed == pytest.approx(thickness, abs=1e-9)
    assert rs.finish == finish and rs.mask.source == "declared"


def test_reader_returns_new_objects_only():
    text = sk.emit(_rs("d722_4l_hybrid"))
    first, second = sk.netlist_stackup_from_board(text), sk.netlist_stackup_from_board(text)
    assert first == second and first is not second
    first["layers"][0]["thickness"] = 99
    assert sk.netlist_stackup_from_board(text)["layers"][0]["thickness"] == 0.035
