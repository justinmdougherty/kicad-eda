"""server/tools/net_equivalence.py: pad-partition connectivity equivalence
(ROADMAP_DECISIONS D4).

Unit tier (no KiCad): the three readers (netlist.json, kicad-cli XML netlist
plus the S-expression netlist, .kicad_pcb text) on committed fixtures under
tests/fixtures/equiv/, and compare() on small synthetic partitions for every
diff category.

The board fixtures ``board_k{5..11}.sexpr`` hold ONE circuit written in each
KiCad release's board syntax (KiCad 5 ``module`` + bare-word nets, KiCad 6/7
``fp_text reference``, KiCad 8/9 ``property "Reference"`` + numbered nets,
KiCad 10 name-only ``(net "VCC")``, KiCad 11 name-only + ``transform``). They
are named ``.sexpr`` because the repo .gitignore drops every ``*.kicad_pcb``;
kicad-cli 9.0.4 loads k5..k9 and the 10.99 nightly loads all seven, and both
export identical pad nets for them (IPC-D-356), so they are real boards.

Also: load_pad_nets_meta, the frozen ``kicad-eda.padnets/1`` reference
(write_reference / read_reference), partly unconnected pads and
real_conflicts. The synthetic ``.kicad_pcb`` text edge cases (duplicate
references included) and the S-expression readers are tests/test_sexpr_read.py.

Live tier: a KiCad demo's schematic netlist exported with that install's
kicad-cli compared with the demo's own board; skipped without KiCad. The
10.99 nightly is found by a session fixture on first use: collecting this
module starts no kicad-cli.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import net_equivalence as ne
from fixtures.kicad_install import KICAD, requires_kicad_cli

FIX = Path(__file__).resolve().parent / "fixtures" / "equiv"
BOARD_VERSIONS = ("k5", "k6", "k7", "k8", "k9", "k10", "k11")

# The board fixtures' pad nets (names unescaped: /A{slash}B -> /A/B). U1.3 has
# no net; the NPTH pad "" is not a pad key; U1 carries two "EP" copper pads.
BOARD = {
    "VCC": {("R1", "1"), ("C1", "1")},
    "Net-(R1-Pad2)": {("R1", "2"), ("R2", "1"), ("U1", "1")},
    "GND": {("R2", "2"), ("C1", "2"), ("U1", "EP"), ("J1", "2"), ("H1", "1")},
    "unconnected-(U1-NC-Pad2)": {("U1", "2")},
    "/A/B": {("U1", "4"), ("J1", "1")},
    ne.NO_NET: {("U1", "3")},
}
# The schematic side: no H1 (board-only), C9 is excluded from the board, C1 is
# DNP (still on the board), every unconnected pin is its own net.
SCHEMATIC = {
    "/A/B": {("J1", "1"), ("U1", "4")},
    "GND": {("C1", "2"), ("J1", "2"), ("R2", "2"), ("U1", "EP")},
    "Net-(R1-Pad2)": {("R1", "2"), ("R2", "1"), ("U1", "1")},
    "VCC": {("C1", "1"), ("R1", "1")},
    "unconnected-(U1-NC-Pad2)": {("U1", "2")},
    "unconnected-(U1-Pad3)": {("U1", "3")},
}


def _text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def _pads(*keys: str) -> tuple[tuple[str, str], ...]:
    """'R1.2' -> ('R1', '2')."""
    return tuple(tuple(k.split(".", 1)) for k in keys)          # type: ignore[misc]


def _nets(**nets: str) -> dict[str, set[tuple[str, str]]]:
    """_nets(GND="R1.1 R2.2") -> {"GND": {("R1","1"), ("R2","2")}}."""
    return {name: set(_pads(*spec.split())) for name, spec in nets.items()}


# ----------------------------------------------------------------------------
# Reader 1: netlist.json
# ----------------------------------------------------------------------------
def test_netlist_reader_normalises_pads_and_files_no_connect_under_no_net() -> None:
    design = json.loads(_text("netlist.json"))
    got = ne.netlist_pad_nets(design)
    assert got == {
        "VCC": {("R1", "1"), ("C1", "1")},                  # int pad 1 -> "1"
        "EN": {("R1", "2"), ("R2", "1"), ("U1", "1")},
        "GND": {("R2", "2"), ("C1", "2"), ("U1", "EP"), ("J1", "2"), ("H1", "1")},
        "A_B": {("U1", "4"), ("J1", "1")},
        ne.NO_NET: {("U1", "2"), ("U1", "3")},
    }
    assert design["nets"]["VCC"][0] == ["R1", 1]               # the input is not mutated


def test_netlist_reader_can_drop_board_only_parts() -> None:
    got = ne.netlist_pad_nets(json.loads(_text("netlist.json")), exclude_board_only=True)
    assert ("H1", "1") not in got["GND"] and len(got["GND"]) == 4


@pytest.mark.parametrize("design, match", [
    ({"nets": [["R1", "1"]]}, "'nets' must be an object"),
    ({"nets": {"GND": [["R1"]]}}, r"nets\['GND'\].*\[ref, pad\]"),
    ({"nets": {"GND": "R1.1"}}, r"nets\['GND'\] must be a list"),
    ({"nets": {"": [["R1", "1"], ["R2", "1"]]}}, "empty net name"),
    ({"nets": {"GND": [["R1", True]]}}, r"\[ref, pad\]"),
    ({"nets": {"GND": [["", "1"]]}}, r"\[ref, pad\]"),
    ({"nets": {}, "no_connect": [["U1"]]}, r"no_connect.*\[ref, pad\]"),
    ({"nets": {}, "components": {"ref": "R1"}}, "'components' must be a list"),
])
def test_netlist_reader_rejects_malformed_input(design: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        ne.netlist_pad_nets(design)


# ----------------------------------------------------------------------------
# Reader 2: kicad-cli netlist exports (XML, and the S-expression .net)
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("fixture", ["sch_k9.xml", "sch_k11.xml"])
def test_xml_reader_drops_board_excluded_parts_and_keeps_dnp(fixture: str) -> None:
    assert ne.xml_pad_nets(_text(fixture)) == SCHEMATIC


def test_xml_reader_options_keep_excluded_or_drop_dnp() -> None:
    kept = ne.xml_pad_nets(_text("sch_k9.xml"), include_excluded=True)
    assert ("C9", "1") in kept["VCC"] and ("C9", "2") in kept["GND"]
    no_dnp = ne.xml_pad_nets(_text("sch_k9.xml"), drop_dnp=True)
    assert ("C1", "1") not in no_dnp["VCC"] and ("C1", "2") not in no_dnp["GND"]


@pytest.mark.parametrize("text, match", [
    ("<root/>", "not a KiCad netlist export"),
    ("<export version='E'><nets>", "not well-formed XML"),
    ("<export version='E'><components/></export>", "has no <nets>"),
    ("<export version='E'><nets><net code='1' name='A'><node ref='R1'/></net></nets></export>",
     "node without ref/pin"),
])
def test_xml_reader_rejects_what_is_not_a_netlist(text: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        ne.xml_pad_nets(text)


def test_xml_reader_names_an_unnamed_net_by_its_code() -> None:
    text = ("<export version='E'><nets><net code='7' name=''>"
            "<node ref='R1' pin='1'/><node ref='R2' pin='1'/></net></nets></export>")
    assert ne.xml_pad_nets(text) == {"<net code 7>": {("R1", "1"), ("R2", "1")}}


def test_sexpr_netlist_reader_matches_the_xml_reader() -> None:
    # KiCad omits board-excluded parts from this format; names are escaped
    assert ne.sexpr_netlist_pad_nets(_text("sch_k9.net")) == SCHEMATIC


def test_sexpr_netlist_reader_honours_flags_when_present() -> None:
    text = """(export (version "E")
      (components
        (comp (ref "C9") (value "10u") (property (name "exclude_from_board")))
        (comp (ref "C1") (value "1u") (property (name "dnp"))))
      (nets
        (net (code "1") (name "VCC") (class "Default")
          (node (ref "C9") (pin "1") (pintype "passive"))
          (node (ref "C1") (pin "1") (pintype "passive"))
          (node (ref "R1") (pin "1") (pintype "passive")))))"""
    assert ne.sexpr_netlist_pad_nets(text) == {"VCC": {("C1", "1"), ("R1", "1")}}
    assert ne.sexpr_netlist_pad_nets(text, drop_dnp=True) == {"VCC": {("R1", "1")}}
    assert len(ne.sexpr_netlist_pad_nets(text, include_excluded=True)["VCC"]) == 3


def test_sexpr_netlist_reader_rejects_a_board() -> None:
    with pytest.raises(ValueError, match="not a KiCad S-expression netlist"):
        ne.sexpr_netlist_pad_nets(_text("board_k9.sexpr"))


# ----------------------------------------------------------------------------
# Reader 3: .kicad_pcb text, KiCad 5 through 11
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("version", BOARD_VERSIONS)
def test_pcb_reader_reads_every_board_format(version: str) -> None:
    assert ne.pcb_pad_nets(_text(f"board_{version}.sexpr")) == BOARD


@pytest.mark.parametrize("version", BOARD_VERSIONS[1:])          # board_only arrived in KiCad 6
def test_pcb_reader_can_drop_board_only_footprints(version: str) -> None:
    got = ne.pcb_pad_nets(_text(f"board_{version}.sexpr"), exclude_board_only=True)
    assert got == {**BOARD, "GND": BOARD["GND"] - {("H1", "1")}}


@pytest.mark.parametrize("version, expected", [
    ("k5", 20171130), ("k6", 20211014), ("k7", 20221018), ("k8", 20240108),
    ("k9", 20241229), ("k10", 20260206), ("k11", 20260901)])
def test_pcb_format_version_reads_the_header(version: str, expected: int) -> None:
    assert ne.pcb_format_version(_text(f"board_{version}.sexpr")) == expected


def test_pcb_reader_survives_crlf_line_endings_and_a_bom() -> None:
    text = chr(0xFEFF) + _text("board_k9.sexpr").replace("\n", "\r\n")
    assert ne.pcb_pad_nets(text) == BOARD


# The synthetic .kicad_pcb text edge cases live in tests/test_sexpr_read.py.


# ----------------------------------------------------------------------------
# load_pad_nets: one entry point for files
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("name, expected", [
    ("board_k10.sexpr", BOARD), ("sch_k11.xml", SCHEMATIC), ("sch_k9.net", SCHEMATIC)])
def test_load_pad_nets_detects_the_format_from_the_content(name: str, expected: dict) -> None:
    assert ne.load_pad_nets(FIX / name) == expected


def test_load_pad_nets_reads_netlist_json_and_passes_the_board_only_switch() -> None:
    assert ("H1", "1") in ne.load_pad_nets(FIX / "netlist.json")["GND"]
    assert ("H1", "1") not in ne.load_pad_nets(FIX / "netlist.json", exclude_board_only=True)["GND"]
    board = ne.load_pad_nets(FIX / "board_k11.sexpr", exclude_board_only=True)
    assert ("H1", "1") not in board["GND"]


def test_load_pad_nets_refuses_a_schematic_and_unknown_content(tmp_path: Path) -> None:
    sch = tmp_path / "x.kicad_sch"
    sch.write_text("(kicad_sch (version 20250114))", encoding="utf-8")
    with pytest.raises(ValueError, match="export_schematic_xml"):
        ne.load_pad_nets(sch)
    renamed = tmp_path / "x.txt"
    renamed.write_text("(kicad_sch (version 20250114))", encoding="utf-8")
    with pytest.raises(ValueError, match="this is a schematic"):
        ne.load_pad_nets(renamed)
    odd = tmp_path / "y.txt"
    odd.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot tell what"):
        ne.load_pad_nets(odd)
    bad_json = tmp_path / "netlist.json"
    bad_json.write_text('{"nets": {"A": [["R1", "1"]]}, "no_connect": {}}', encoding="utf-8")
    with pytest.raises(ValueError, match=r"netlist\.json: netlist 'no_connect' must be a list"):
        ne.load_pad_nets(bad_json)
    with pytest.raises(FileNotFoundError):
        ne.load_pad_nets(tmp_path / "missing.kicad_pcb")


# ----------------------------------------------------------------------------
# load_pad_nets_meta (a board with duplicate references: tests/test_sexpr_read.py)
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("name, kind, version, board_only", [
    ("board_k5.sexpr", "board", 20171130, []), ("board_k11.sexpr", "board", 20260901, ["H1"]),
    ("sch_k9.xml", "xml", "E", []), ("sch_k9.net", "net", "E", []),
    ("netlist.json", "netlist", None, ["H1"])])
def test_load_pad_nets_meta_names_the_kind(name: str, kind: str, version: object,
                                           board_only: list) -> None:
    nets, meta = ne.load_pad_nets_meta(FIX / name)
    assert nets == ne.load_pad_nets(FIX / name)
    assert meta == {"kind": kind, "format_version": version, "duplicate_refs": [],
                    "board_only_refs": board_only}


# ----------------------------------------------------------------------------
# The frozen pad-net reference (kicad-eda.padnets/1)
# ----------------------------------------------------------------------------
_SOURCE = {"kind": "kicad", "path": "src/x.kicad_pcb", "sha256": "ab12"}


def test_write_reference_round_trips(tmp_path: Path) -> None:
    nets = {**BOARD, "Ω µ": {("Q10", "G"), ("Q2", "G")}, "VCC": BOARD["VCC"] | {("U1", "2")}}
    path = ne.write_reference(tmp_path / "out" / "ref.json", nets, board_only=["H1"],
                              duplicate_refs=("STITCH1", "POLY", "STITCH1"), source=_SOURCE)
    raw = path.read_bytes()
    assert b"\r" not in raw and "Ω µ".encode() in raw and raw.endswith(b"}\n")
    data = json.loads(raw)
    assert data["format"] == "kicad-eda.padnets/1" and data["source"] == _SOURCE
    assert data["board_only"] == ["H1"] and data["duplicate_refs"] == ["POLY", "STITCH1"]
    assert list(data["nets"]) == ["", "/A/B", "GND", "Net-(R1-Pad2)", "VCC",
                                  "unconnected-(U1-NC-Pad2)", "Ω µ"]
    assert data["nets"][""] == [["U1", "3"]] and data["nets"]["Ω µ"] == [["Q2", "G"], ["Q10", "G"]]
    got, meta = ne.read_reference(path)
    assert got == nets
    assert meta == {"kind": "reference", "format_version": "kicad-eda.padnets/1",
                    "duplicate_refs": ["POLY", "STITCH1"], "board_only_refs": ["H1"],
                    "source": _SOURCE}
    assert ne.load_pad_nets_meta(path) == (got, meta) and ne.load_pad_nets(path) == got
    assert ne.load_pad_nets(path, exclude_board_only=True) == {
        **nets, "GND": nets["GND"] - {("H1", "1")}}
    again = ne.write_reference(tmp_path / "again.json", got, board_only=meta["board_only_refs"],
                               duplicate_refs=meta["duplicate_refs"], source=meta["source"])
    assert again.read_bytes() == raw


def test_a_reference_compares_like_its_source(tmp_path: Path) -> None:
    path = ne.write_reference(tmp_path / "ref.json", ne.load_pad_nets(FIX / "board_k11.sexpr"))
    assert json.loads(path.read_text(encoding="utf-8"))["source"] == {}
    res = ne.compare(ne.load_pad_nets(FIX / "netlist.json"), ne.load_pad_nets(path))
    assert res.equal and ne.read_reference(path)[1]["board_only_refs"] == []


_FMT = "kicad-eda.padnets/1"


@pytest.mark.parametrize("doc, match", [
    ({"format": "kicad-eda.padnets/2", "nets": {}}, "kicad-eda.padnets/1"),
    ([_FMT], "kicad-eda.padnets/1"),
    ({"format": _FMT, "nets": []}, "'nets' must be an object"),
    ({"format": _FMT, "nets": {"A": "R1.1"}}, "must be a list"),
    ({"format": _FMT, "nets": {"A": [["R1"]]}}, r"\[ref, pad\]"),
    ({"format": _FMT, "nets": {}, "board_only": "H1"}, "board_only"),
    ({"format": _FMT, "nets": {}, "duplicate_refs": [1]}, "duplicate_refs"),
    ({"format": _FMT, "nets": {}, "source": ["x"]}, "source"),
])
def test_read_reference_rejects_malformed_files(tmp_path: Path, doc: object, match: str) -> None:
    path = tmp_path / "ref.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        ne.read_reference(path)
    if isinstance(doc, dict):
        with pytest.raises(ValueError, match=match):
            ne.load_pad_nets(path)


def test_reference_io_fails_loudly(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("{nope", encoding="utf-8")
    with pytest.raises(ValueError, match="bad.json"):
        ne.read_reference(tmp_path / "bad.json")
    out = tmp_path / "w.json"
    for kw, match in (({"pad_nets": {"A": [["R1"]]}}, r"\[ref, pad\]"),
                      ({"pad_nets": {}, "board_only": "H1"}, "board_only"),
                      ({"pad_nets": {}, "source": ["x"]}, "source"),
                      ({"pad_nets": {}, "source": {"x": object()}}, "source")):
        with pytest.raises(ValueError, match=match):
            ne.write_reference(out, **kw)
    assert not out.exists()


# ----------------------------------------------------------------------------
# compare(): every diff category on small synthetic partitions
# ----------------------------------------------------------------------------
def test_identical_partitions_are_equal() -> None:
    a = _nets(GND="R1.2 C1.2 U1.4", VCC="R1.1 C1.1")
    res = ne.compare(a, a)
    assert res.equal
    assert not (res.split_nets or res.merged_nets or res.missing_pins or res.extra_pins)
    assert res.details["net_name_map"] == {"GND": "GND", "VCC": "VCC"}
    assert res.details["nets_a"] == res.details["nets_b"] == 2
    assert res.details["common_pads"] == 5
    assert res.summary().startswith("equivalent")


def test_net_names_never_matter() -> None:
    a = _nets(GND="R1.2 C1.2", VCC="R1.1 C1.1")
    b = _nets(**{"Net-(C1-Pad2)": "R1.2 C1.2", "+3V3": "R1.1 C1.1"})
    res = ne.compare(a, b)
    assert res.equal
    assert res.details["net_name_map"] == {"GND": "Net-(C1-Pad2)", "VCC": "+3V3"}


def test_split_net_is_reported_with_its_fragments() -> None:
    a = _nets(GND="R1.2 C1.2 U1.4 U1.5")
    b = _nets(GND="R1.2 C1.2", AGND="U1.4 U1.5")
    res = ne.compare(a, b)
    assert not res.equal and not res.merged_nets and not res.missing_pins and not res.extra_pins
    (split,) = res.split_nets
    assert split.net == "GND"
    assert split.pads == _pads("C1.2", "R1.2", "U1.4", "U1.5")
    assert [(p.net, p.pads) for p in split.parts] == [
        ("GND", _pads("C1.2", "R1.2")), ("AGND", _pads("U1.4", "U1.5"))]
    assert res.details["net_name_map"] == {}


def test_a_pad_cut_loose_is_a_split_into_an_isolated_pad() -> None:
    a = _nets(GND="R1.2 C1.2 U1.4", SIG="R2.1 R3.1")
    b = {"GND": {("R1", "2"), ("C1", "2")}, ne.NO_NET: {("U1", "4")},
         "SIG": {("R2", "1")}, "unconnected-(R3-Pad1)": {("R3", "1")}}
    res = ne.compare(a, b)
    by_net = {s.net: s for s in res.split_nets}
    assert [(p.net, p.pads) for p in by_net["GND"].parts] == [
        ("GND", _pads("C1.2", "R1.2")), (None, _pads("U1.4"))]        # None: no net at all
    assert [(p.net, p.pads) for p in by_net["SIG"].parts] == [
        ("SIG", _pads("R2.1")), ("unconnected-(R3-Pad1)", _pads("R3.1"))]
    assert not res.missing_pins and not res.merged_nets


def test_merged_nets_are_reported_with_their_sources() -> None:
    a = _nets(GND="R1.2 C1.2", AGND="U1.4 U1.5")
    b = _nets(GND="R1.2 C1.2 U1.4 U1.5")
    res = ne.compare(a, b)
    assert not res.equal and not res.split_nets
    (merged,) = res.merged_nets
    assert merged.net == "GND"
    assert [(p.net, p.pads) for p in merged.parts] == [
        ("GND", _pads("C1.2", "R1.2")), ("AGND", _pads("U1.4", "U1.5"))]


def test_a_formerly_isolated_pad_joining_a_net_is_a_merge() -> None:
    a = {"GND": {("R1", "2"), ("C1", "2")}, "unconnected-(U1-Pad3)": {("U1", "3")}}
    b = _nets(GND="R1.2 C1.2 U1.3")
    (merged,) = ne.compare(a, b).merged_nets
    assert [(p.net, p.pads) for p in merged.parts] == [
        ("GND", _pads("C1.2", "R1.2")), ("unconnected-(U1-Pad3)", _pads("U1.3"))]


def test_crossed_nets_are_both_split_and_merged() -> None:
    a = _nets(X="R1.1 R2.1", Y="R3.1 R4.1")
    b = _nets(P="R1.1 R3.1", Q="R2.1 R4.1")
    res = ne.compare(a, b)
    assert sorted(s.net for s in res.split_nets) == ["X", "Y"]
    assert sorted(m.net for m in res.merged_nets) == ["P", "Q"]


def test_missing_pins_and_missing_refs() -> None:
    a = _nets(GND="R1.2 C1.2 C2.2 U1.4", VCC="R1.1 C1.1 C2.1")
    b = _nets(GND="R1.2 C1.2", VCC="R1.1 C1.1 U1.9")
    res = ne.compare(a, b)
    assert res.missing_pins == _pads("C2.1", "C2.2", "U1.4")
    assert res.details["missing_refs"] == ["C2"]                     # U1 exists in b (pad 9)
    assert res.details["missing_by_net"] == {"GND": [["C2", "2"], ["U1", "4"]],
                                             "VCC": [["C2", "1"]]}
    assert res.extra_pins == _pads("U1.9")
    assert not res.split_nets and not res.merged_nets               # the common pads agree
    assert not res.equal


def test_extra_pins_and_ignore_refs() -> None:
    a = _nets(GND="R1.2 C1.2")
    b = _nets(GND="R1.2 C1.2 H1.1 H2.1")
    res = ne.compare(a, b)
    assert res.extra_pins == _pads("H1.1", "H2.1")
    assert res.details["extra_refs"] == ["H1", "H2"]
    assert res.details["extra_by_net"] == {"GND": [["H1", "1"], ["H2", "1"]]}
    assert not res.split_nets and not res.merged_nets and not res.equal
    ok = ne.compare(a, b, ignore_refs=["H1", "H2", "H9"])
    assert ok.equal and ok.details["ignored_refs"] == ["H1", "H2"]


def test_natural_ordering_of_pads() -> None:
    a = _nets(N="R10.1 R2.1 R1.10 R1.2")
    b = _nets(N="R10.1 R2.1", M="R1.10 R1.2")
    (split,) = ne.compare(a, b).split_nets
    assert split.pads == _pads("R1.2", "R1.10", "R2.1", "R10.1")


def test_ignore_single_pin_treats_isolated_pads_as_optional() -> None:
    a = {"GND": {("R1", "2"), ("C1", "2")}, "unconnected-(U1-Pad3)": {("U1", "3")}}
    b = {"GND": {("R1", "2"), ("C1", "2")}, ne.NO_NET: {("J1", "MP")}}
    assert ne.compare(a, b).equal                                  # default: ignore_single_pin=True
    strict = ne.compare(a, b, ignore_single_pin=False)
    assert strict.missing_pins == _pads("U1.3") and strict.extra_pins == _pads("J1.MP")
    assert not strict.split_nets and not strict.merged_nets and not strict.equal


def test_an_isolated_pad_and_a_single_pin_net_are_the_same_thing() -> None:
    a = {"GND": {("R1", "2"), ("C1", "2")}, "unconnected-(U1-Pad3)": {("U1", "3")}}
    b = {"GND": {("R1", "2"), ("C1", "2")}, ne.NO_NET: {("U1", "3")}}
    assert ne.compare(a, b, ignore_single_pin=False).equal


def test_ref_map_renames_a_references_before_comparing() -> None:
    a = _nets(GND="R1.2 C1.2 U1.4", VCC="R1.1 C1.1")
    b = _nets(GND="R11.2 C7.2 U1.4", VCC="R11.1 C7.1")
    assert not ne.compare(a, b).equal
    res = ne.compare(a, b, ref_map={"R1": "R11", "C1": "C7", "Q9": "Q1"})
    assert res.equal
    assert res.details["ref_map_unused"] == ["Q9"]


def test_ref_map_can_swap_references() -> None:
    a = _nets(GND="R1.2 R2.2", VCC="R1.1", SIG="R2.1 U1.1")
    b = _nets(GND="R2.2 R1.2", VCC="R2.1", SIG="R1.1 U1.1")
    assert not ne.compare(a, b).equal
    assert ne.compare(a, b, ref_map={"R1": "R2", "R2": "R1"}).equal


@pytest.mark.parametrize("ref_map, match", [
    ({"R1": "R5", "R2": "R5"}, "both to R5"),
    ({"R1": "R2"}, "R1 onto R2, which a still uses"),
    ({"R1": ""}, "empty reference"),
    (["R1", "R2"], "ref_map must be a mapping"),
])
def test_ref_map_must_be_one_to_one(ref_map: object, match: str) -> None:
    a = _nets(GND="R1.2 R2.2")
    with pytest.raises((ValueError, TypeError), match=match):
        ne.compare(a, a, ref_map=ref_map)                          # type: ignore[arg-type]


def test_a_pad_on_two_nets_joins_them_and_is_reported() -> None:
    a = _nets(GND="R1.2 U1.25", VCC="R1.1 C1.1")
    b = _nets(GND="R1.2 U1.25", VCC="R1.1 C1.1 U1.25")             # U1.25 copper on both nets
    res = ne.compare(a, b)
    assert res.details["pad_conflicts_b"] == [{"pad": ["U1", "25"], "nets": ["GND", "VCC"],
                                               "single_pin": False}]
    (merged,) = res.merged_nets
    assert merged.net == "GND + VCC"
    assert sorted(p.net for p in merged.parts) == ["GND", "VCC"]
    assert not res.equal


def test_the_same_conflict_on_both_sides_is_still_equivalent() -> None:
    a = _nets(GND="R1.2 U1.25", VCC="R1.1 U1.25")
    res = ne.compare(a, a)
    assert res.equal and res.details["pad_conflicts_a"] == res.details["pad_conflicts_b"]


def test_real_conflicts_drops_pads_alone_on_unconnected_nets() -> None:
    # cm5_minima J502: two copper 'MP' pads, each alone on its own unconnected-(...) net
    nets = {"unconnected-(J502-MountPin-PadMP)": {("J502", "MP")},
            "unconnected-(J502-MountPin-PadMP)_1": {("J502", "MP")},
            "unconnected-(X)": {("U3", "1"), ("R5", "1")}, "unconnected-(X)_1": {("U3", "1")},
            "A": {("U2", "5")}, "B": {("U2", "5")}, **_nets(GND="R1.2 U1.25", VCC="R1.1 U1.25")}
    res = ne.compare(nets, nets)
    assert res.equal and len(res.details["pad_conflicts_b"]) == 4
    kept = [{"pad": ["U1", "25"], "nets": ["GND", "VCC"], "single_pin": False},
            {"pad": ["U2", "5"], "nets": ["A", "B"], "single_pin": True},
            {"pad": ["U3", "1"], "nets": ["unconnected-(X)", "unconnected-(X)_1"],
             "single_pin": False}]
    assert ne.real_conflicts(res.details) == {"pad_conflicts_a": kept, "pad_conflicts_b": kept}
    assert ne.real_conflicts(json.loads(json.dumps(res.as_dict()))["details"])["pad_conflicts_a"] \
        == kept


def test_a_pad_partly_on_no_net_is_listed_and_still_equal() -> None:
    # U1 'EP': one copper pad on GND, a second of the same number on no net
    board = ne.pcb_pad_nets("""(kicad_pcb (version 20260206)
      (footprint "QFN" (property "Reference" "U1") (pad "1" smd (net "GND"))
        (pad "EP" smd rect (net "GND")) (pad "EP" smd rect)))""")
    assert board == {"GND": {("U1", "EP"), ("U1", "1")}, ne.NO_NET: {("U1", "EP")}}
    partly = [{"pad": ["U1", "EP"], "nets": ["GND"]}]
    res = ne.compare(board, board)
    assert res.equal and res.details["partly_unconnected_pads_a"] == partly
    res = ne.compare({"GND": {("U1", "EP"), ("U1", "1")}}, board)
    assert res.equal and res.details["partly_unconnected_pads_a"] == []
    assert res.details["partly_unconnected_pads_b"] == partly


def test_compare_accepts_json_style_lists_and_rejects_garbage() -> None:
    a = {"GND": [["R1", 2], ["C1", "2"]]}
    assert ne.compare(a, {"GND": {("R1", "2"), ("C1", "2")}}).equal
    with pytest.raises(ValueError, match=r"a net 'GND'.*\[ref, pad\]"):
        ne.compare({"GND": [["R1"]]}, a)
    with pytest.raises(ValueError, match="must be a mapping"):
        ne.compare([("R1", "1")], a)                                # type: ignore[arg-type]
    with pytest.raises(ValueError, match="b net 'GND' must be a collection"):
        ne.compare(a, {"GND": "R1.1"})
    with pytest.raises(ValueError, match="design must be an object"):
        ne.netlist_pad_nets([])                                     # type: ignore[arg-type]


def test_result_serialises_to_json() -> None:
    a = _nets(GND="R1.2 C1.2 U1.4", VCC="R1.1 C1.1")
    b = _nets(GND="R1.2 C1.2", AGND="U1.4 H1.1", VCC="R1.1 C1.1 C2.1")
    res = ne.compare(a, b)
    data = json.loads(json.dumps(res.as_dict()))
    assert data["equal"] is False
    assert data["split_nets"] == [{"net": "GND", "pads": [["C1", "2"], ["R1", "2"], ["U1", "4"]],
                                   "parts": [{"net": "GND", "pads": [["C1", "2"], ["R1", "2"]]},
                                             {"net": "AGND", "pads": [["U1", "4"]]}]}]
    assert data["extra_pins"] == [["C2", "1"], ["H1", "1"]]
    assert "1 split" in res.summary() and "2 extra" in res.summary()


# ----------------------------------------------------------------------------
# The fixtures against each other (readers + compare, no KiCad)
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("version", BOARD_VERSIONS)
def test_netlist_json_matches_every_board_format(version: str) -> None:
    design = ne.load_pad_nets(FIX / "netlist.json")
    res = ne.compare(design, ne.load_pad_nets(FIX / f"board_{version}.sexpr"))
    assert res.equal, res.as_dict()
    assert res.details["net_name_map"] == {"A_B": "/A/B", "EN": "Net-(R1-Pad2)", "GND": "GND",
                                           "VCC": "VCC"}


@pytest.mark.parametrize("sch", ["sch_k9.xml", "sch_k11.xml", "sch_k9.net"])
def test_schematic_against_board_differs_only_by_the_board_only_hole(sch: str) -> None:
    schematic = ne.load_pad_nets(FIX / sch)
    res = ne.compare(schematic, ne.load_pad_nets(FIX / "board_k11.sexpr"))
    assert not res.equal and res.extra_pins == _pads("H1.1") and res.details["extra_refs"] == ["H1"]
    assert not res.split_nets and not res.merged_nets and not res.missing_pins
    board = ne.load_pad_nets(FIX / "board_k11.sexpr", exclude_board_only=True)
    assert ne.compare(schematic, board).equal


# ----------------------------------------------------------------------------
# export_schematic_xml (kicad-cli runner), with subprocess.run faked
# ----------------------------------------------------------------------------
class _FakeRun:
    def __init__(self, rc: int = 0, stdout: str = "", stderr: str = "", write: bool = True,
                 raises: BaseException | None = None) -> None:
        self.rc, self.stdout, self.stderr = rc, stdout, stderr
        self.write, self.raises = write, raises
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv: list[str], **kw: object) -> subprocess.CompletedProcess:
        self.calls.append((argv, kw))
        if self.raises is not None:
            raise self.raises
        if self.write:
            Path(argv[argv.index("-o") + 1]).write_text("<export/>", encoding="utf-8")
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, self.stderr)


def test_export_schematic_xml_runs_kicad_cli_detached(tmp_path: Path, monkeypatch) -> None:
    # a real 10.99 export prints this; it is not an error
    fake = _FakeRun(stdout="18:29:24: Font 'Avenir Black' not found; substituting 'Verdana'.\n")
    monkeypatch.setattr(ne.subprocess, "run", fake)
    out = ne.export_schematic_xml(tmp_path / "a.kicad_sch", tmp_path / "o" / "a.xml", "kicad-cli")
    assert out == tmp_path / "o" / "a.xml" and out.is_file()
    (argv, kw), = fake.calls
    assert argv == ["kicad-cli", "sch", "export", "netlist", "--format", "kicadxml",
                    "-o", str(out), str(tmp_path / "a.kicad_sch")]
    assert kw["stdin"] is subprocess.DEVNULL and kw["timeout"] > 0


@pytest.mark.parametrize("fake, match", [
    (_FakeRun(rc=1, stderr="Failed to load schematic"), r"exit 1.*Failed to load schematic"),
    (_FakeRun(stderr="Error: library 'X' not found"), "Error: library 'X' not found"),
    (_FakeRun(write=False), "wrote no"),
    (_FakeRun(raises=subprocess.TimeoutExpired("kicad-cli", 5)), "timed out"),
    (_FakeRun(raises=FileNotFoundError("no such file")), "cannot start"),
])
def test_export_schematic_xml_fails_loudly(tmp_path: Path, monkeypatch, fake: _FakeRun,
                                           match: str) -> None:
    out = tmp_path / "a.xml"
    out.write_text("stale", encoding="utf-8")                      # must not pass for fresh output
    monkeypatch.setattr(ne.subprocess, "run", fake)
    with pytest.raises(RuntimeError, match=match):
        ne.export_schematic_xml(tmp_path / "a.kicad_sch", out, "kicad-cli", timeout_s=5)


# ----------------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------------
def test_cli_exit_codes_and_json(capsys) -> None:
    assert ne.main([str(FIX / "netlist.json"), str(FIX / "board_k8.sexpr")]) == 0
    assert "equivalent" in capsys.readouterr().out
    assert ne.main([str(FIX / "sch_k9.xml"), str(FIX / "board_k8.sexpr"), "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["extra_pins"] == [["H1", "1"]]
    assert ne.main([str(FIX / "sch_k9.xml"), str(FIX / "board_k8.sexpr"),
                    "--exclude-board-only"]) == 0
    assert ne.main([str(FIX / "sch_k9.xml"), str(FIX / "board_k8.sexpr"), "--ignore-ref", "H1",
                    "--ref-map", '{"R1": "R1"}']) == 0
    assert ne.main([str(FIX / "sch_k9.xml"), str(FIX / "nope.kicad_pcb")]) == 2
    assert "nope.kicad_pcb" in capsys.readouterr().err


def test_cli_asks_for_kicad_cli_to_read_a_schematic(tmp_path: Path, capsys) -> None:
    (tmp_path / "x.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    assert ne.main([str(tmp_path / "x.kicad_sch"), str(FIX / "board_k8.sexpr")]) == 2
    assert ("x.kicad_sch is a schematic: pass --kicad-cli <path to kicad-cli> to export it"
            in capsys.readouterr().err)


def test_cli_describes_the_differences_and_exports_a_schematic(tmp_path: Path, capsys,
                                                               monkeypatch) -> None:
    calls: list[tuple] = []

    def fake_export(sch: Path, out: Path, cli: str) -> Path:
        calls.append((sch, cli))
        shutil.copy(FIX / "sch_k9.xml", out)
        return out

    monkeypatch.setattr(ne, "export_schematic_xml", fake_export)
    (tmp_path / "a.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    ref_map = tmp_path / "map.json"
    ref_map.write_text('{"R1": "R7"}', encoding="utf-8")
    code = ne.main([str(tmp_path / "a.kicad_sch"), str(FIX / "board_k8.sexpr"),
                    "--kicad-cli", "kicad-cli", "--ref-map", str(ref_map)])
    out = capsys.readouterr().out
    assert code == 1 and calls == [(tmp_path / "a.kicad_sch", "kicad-cli")]
    assert out.startswith("NOT equivalent")
    assert "missing (2): R7.1 R7.2" in out and "extra (3): H1.1 R1.1 R1.2" in out


# ----------------------------------------------------------------------------
# Live tier: a KiCad demo's schematic (exported by that install's kicad-cli)
# against the demo's own board
# ----------------------------------------------------------------------------
# Keep the project libraries: with its footprint library resolvable, the
# 10.99 exporter leaves out symbol pins that have no pad in the footprint
# (cm5_minima J701 M1..M4); without it those pins appear as nets.
_NOT_NEEDED = shutil.ignore_patterns("*.pdf", "*.step", "*.stp", "*.wrl", "*.3dshapes",
                                     "*.png", "*.jpg", "*.svg", "*.zip", "doc", "pcba", "3d_lib")


def _demo_copy(root: Path, demo: str, tmp_path: Path) -> Path:
    src = root / "share" / "kicad" / "demos" / demo
    if not src.is_dir():
        pytest.skip(f"demo {demo} is not installed under {root}")
    dst = tmp_path / demo                    # kicad-cli may write .kicad_prl beside the project
    shutil.copytree(src, dst, ignore=_NOT_NEEDED)
    return dst


def _sch_vs_pcb(cli: Path, project: Path, stem: str, tmp_path: Path,
                **kw: object) -> ne.EquivalenceResult:
    xml = ne.export_schematic_xml(project / f"{stem}.kicad_sch", tmp_path / f"{stem}.xml", cli)
    board = ne.load_pad_nets(project / f"{stem}.kicad_pcb")
    return ne.compare(ne.load_pad_nets(xml), board, **kw)


def _explain(res: ne.EquivalenceResult) -> str:
    return res.summary() + "\n" + json.dumps(res.as_dict(), indent=1)[:4000]


@requires_kicad_cli
@pytest.mark.parametrize("demo, stem", [
    ("pic_programmer", "pic_programmer"),
    ("complex_hierarchy", "complex_hierarchy"),     # one sheet instanced twice
    ("multichannel", "multichannel_mixer"),         # one sheet instanced four times
    ("stickhub", "StickHub"),                       # project-local libraries, B-side parts
])
def test_live_demo_schematic_matches_its_board(tmp_path: Path, demo: str, stem: str) -> None:
    project = _demo_copy(KICAD.root, demo, tmp_path)
    res = _sch_vs_pcb(KICAD.cli, project, stem, tmp_path)
    assert res.equal, _explain(res)
    assert res.details["nets_a"] > 20 and res.details["common_pads"] > 100
    assert len(res.details["net_name_map"]) == res.details["nets_a"]


@requires_kicad_cli
def test_live_board_only_parts_are_the_only_difference(tmp_path: Path) -> None:
    """tiny_tapeout: the board adds STITCH* stitching footprints on GND that
    are "not in schematic" (board_only). They are the whole difference."""
    project = _demo_copy(KICAD.root, "tiny_tapeout", tmp_path)
    res = _sch_vs_pcb(KICAD.cli, project, "tinytapeout-demo", tmp_path)
    pcb = project / "tinytapeout-demo.kicad_pcb"
    refs = lambda nets: {ref for keys in nets.values() for ref, _ in keys}        # noqa: E731
    board_only = refs(ne.load_pad_nets(pcb)) - refs(ne.load_pad_nets(pcb, exclude_board_only=True))
    assert not (res.split_nets or res.merged_nets or res.missing_pins), _explain(res)
    assert {ref for ref, _ in res.extra_pins} <= board_only, _explain(res)
    assert _sch_vs_pcb(KICAD.cli, project, "tinytapeout-demo", tmp_path,
                       ignore_refs=board_only).equal
    meta = ne.load_pad_nets_meta(pcb)[1]            # 130 stitching vias all called STITCH1
    assert "STITCH1" in meta["duplicate_refs"] and "STITCH1" in meta["board_only_refs"]


@requires_kicad_cli
def test_live_footprint_missing_symbol_pads_is_reported(tmp_path: Path) -> None:
    """vme-wren (KiCad 9 demo): the SFP+ cage Cage3 has 20 GND pins in the
    schematic but its board footprint has only pad 2. KiCad's own
    `pcb drc --schematic-parity` reports the same 19 pins ("No pad found for
    pin N"); a later demo may fix it, so equal is accepted too."""
    project = _demo_copy(KICAD.root, "vme-wren", tmp_path)
    res = _sch_vs_pcb(KICAD.cli, project, "vme-wren", tmp_path)
    if res.equal:
        return
    expected = tuple(("Cage3", str(n)) for n in [1, *range(3, 21)])
    assert res.missing_pins == expected, _explain(res)
    assert not (res.split_nets or res.merged_nets or res.extra_pins), _explain(res)
    assert res.details["missing_by_net"] == {"GND": [list(k) for k in expected]}
    assert res.details["missing_refs"] == []                   # Cage3 itself is on the board


@pytest.fixture(scope="session")
def nightly() -> Path:
    """A kicad-cli >= 10.99 (it reads the KiCad 10 format demo boards),
    probed on first use: collecting this module runs no kicad-cli."""
    import kicad_env as env
    for cli in env.candidate_clis():
        version = env.cli_version(cli)
        if version and env.parse_version(version) >= (10, 99, 0):
            return cli
    pytest.skip("no KiCad 11 nightly (10.99) kicad-cli installed")


def test_collecting_this_module_runs_no_kicad_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    import kicad_env as env

    def trap(*args: object, **kw: object) -> None:
        raise AssertionError(f"a probe ran at import: {args!r}")

    for owner, name in ((subprocess, "run"), (subprocess, "Popen"), (env, "candidate_clis"),
                        (env, "cli_version"), (env, "discover")):
        monkeypatch.setattr(owner, name, trap)
    spec = importlib.util.spec_from_file_location("_net_equivalence_collect", __file__)
    spec.loader.exec_module(importlib.util.module_from_spec(spec))


def test_live_nightly_kicad10_format_board_matches_its_schematic(tmp_path: Path,
                                                                 nightly: Path) -> None:
    """The nightly's pic_programmer board is KiCad 10 format: name-only pad
    nets and no board net table."""
    project = _demo_copy(nightly.parent.parent, "pic_programmer", tmp_path)
    pcb = project / "pic_programmer.kicad_pcb"
    if (ne.pcb_format_version(pcb.read_text(encoding="utf-8")) or 0) < 20260206:
        pytest.skip("this nightly ships a pre-KiCad-10 pic_programmer board")
    res = _sch_vs_pcb(nightly, project, "pic_programmer", tmp_path)
    assert res.equal, _explain(res)


@pytest.mark.parametrize("demo, stem", [("stickhub", "StickHub"), ("cm5_minima", "CM5_MINIMA_3")])
def test_live_nightly_demos_match(tmp_path: Path, nightly: Path, demo: str, stem: str) -> None:
    project = _demo_copy(nightly.parent.parent, demo, tmp_path)
    res = _sch_vs_pcb(nightly, project, stem, tmp_path)
    assert res.equal, _explain(res)
    if demo == "cm5_minima":                    # J502/J503 'MP': harmless, not real conflicts
        board = ne.load_pad_nets(project / f"{stem}.kicad_pcb")
        assert ne.real_conflicts(ne.compare(board, board).details)["pad_conflicts_a"] == []
