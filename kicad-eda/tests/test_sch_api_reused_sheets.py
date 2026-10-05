"""backends.sch_api / sch_items — pins on reused hierarchical sheets.

A sheet file used N times (KiCad's multichannel demo: channel_strip.kicad_sch
four times) is ONE screen: its symbols and their pins keep the same KIIDs in
every instance, while each instance has its own sheet path and its own
reference (R12 on /CH1/, R23 on /CH2/, ...). GetSchematicNetlist lists a net's
pin KIIDs per sheet path, so a pin is only identified by (sheet path, KIID).

Pure tier: fakes with shared KIIDs across two sheet paths (no KiCad, no kipy).
Live tier (KiCad 11 nightly headless api-server; skipped without it): a scratch
copy of demos/multichannel — SchematicApi.netlist() as pad nets equals the
kicad-cli XML export's pad nets (net_equivalence.compare(...).equal).
"""
from __future__ import annotations

import copy
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import kicad_server
from backends import sch_items as items
from backends.errors import PinNotFoundError
from backends.sch_api import SchematicApi

ROOT_ID = "root-uuid"
SHEET_A = (ROOT_ID, "sheet-a")              # /CH1/
SHEET_B = (ROOT_ID, "sheet-b")              # /CH2/: the same file instanced again
ROOT = (ROOT_ID,)


# ============================================================================
# fakes shaped like the kipy wrappers the backend reads
# ============================================================================
def _kiid(value: str) -> SimpleNamespace:
    return SimpleNamespace(value=value)


def _path(key: tuple[str, ...], human: str = "") -> SimpleNamespace:
    return SimpleNamespace(path=[_kiid(v) for v in key], path_human_readable=human)


def _field(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=SimpleNamespace(value=text), name="", visible=False,
                           allow_auto_place=True)


def _pin(kiid: str, number: str) -> SimpleNamespace:
    return SimpleNamespace(id=_kiid(kiid), number=number, name=f"P{number}", electrical_type=-1)


def _inst(ref: str, sheet: tuple[str, ...], symbol_id: str,
          pins: list[tuple[str, str]]) -> SimpleNamespace:
    """A placed symbol: its definition's pins (KIID, number) are what a reused
    sheet shares between its instances; ``ref`` and ``sheet`` are per instance."""
    children = [SimpleNamespace(kind="SchematicPin", item=_pin(k, n), unit=None) for k, n in pins]
    definition = SimpleNamespace(items=children, description_field=_field("Resistor"),
                                 unit_count=1, id=SimpleNamespace(library="Device", name="R"))
    return SimpleNamespace(reference=ref, path=_path(sheet), id=_kiid(symbol_id), unit=1,
                           value="10k", definition=definition, footprint_field=_field(""),
                           description_field=_field("Resistor"))


def _sheet(key: tuple[str, ...], kiids: list[str]) -> SimpleNamespace:
    return SimpleNamespace(path=_path(key), items=[_kiid(k) for k in kiids])


def _net(name: str, *sheets: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(name=name, sheets=list(sheets))


R_PINS = [("pin-r-1", "1"), ("pin-r-2", "2")]          # one screen: the same pin KIIDs ...
CH1_R = _inst("R1", SHEET_A, "sym-r", R_PINS)            # ... under two sheet paths
CH2_R = _inst("R101", SHEET_B, "sym-r", R_PINS)
PWR = _inst("#PWR01", ROOT, "sym-pwr", [("pin-pwr", "1")])
INSTANCES = [CH1_R, CH2_R, PWR]


class FakeSchematic:
    """get_symbols / get_netlist / update_items like a headless api-server."""

    def __init__(self, symbols: list, nets: list) -> None:
        self._symbols = symbols
        self.nets = nets

    def get_symbols(self) -> list:
        return [copy.deepcopy(s) for s in self._symbols]

    def get_netlist(self) -> list:
        return self.nets

    def update_items(self, inst) -> list:
        return [copy.deepcopy(inst)]


def _api(symbols: list, nets: list) -> SchematicApi:
    sch = FakeSchematic(symbols, nets)
    session = SimpleNamespace(mode="headless", schematic_for=lambda _p: sch, close=lambda: None,
                              close_all_documents=lambda: None)
    return SchematicApi(session, "C:/fake/multi.kicad_sch", owns_session=False)


# KiCad's netlist of the two channels: a wire KIID rides along (not a pin)
CHANNEL_NETS = [
    _net("/CH1/OUT", _sheet(SHEET_A, ["pin-r-2", "wire-1"])),
    _net("/CH2/OUT", _sheet(SHEET_B, ["pin-r-2", "wire-1"])),
    _net("GND", _sheet(ROOT, ["pin-pwr"]), _sheet(SHEET_A, ["pin-r-1"]),
         _sheet(SHEET_B, ["pin-r-1"])),
]


# ============================================================================
# pure tier
# ============================================================================
def test_pin_map_keys_pins_by_sheet_path_and_kiid():
    pins = items.pin_map(INSTANCES)
    assert pins == {(SHEET_A, "pin-r-1"): ("R1", "1"), (SHEET_A, "pin-r-2"): ("R1", "2"),
                    (SHEET_B, "pin-r-1"): ("R101", "1"), (SHEET_B, "pin-r-2"): ("R101", "2"),
                    (ROOT, "pin-pwr"): ("#PWR01", "1")}


def test_instance_sheet_is_the_sheet_path_even_when_it_names_the_symbol():
    assert items.sheet_key(CH1_R.path) == SHEET_A
    assert items.instance_sheet(CH1_R) == SHEET_A
    # SheetPath may also be "the path to a symbol instance": cut back to its sheet
    named = _inst("R1", (*SHEET_A, "sym-r"), "sym-r", R_PINS)
    assert items.instance_sheet(named) == SHEET_A
    assert items.instance_key(named) == items.instance_key(CH1_R) == (SHEET_A, "sym-r")
    assert items.instance_key(CH2_R) == (SHEET_B, "sym-r")


def test_kiid_index_lists_every_pin_a_kiid_stands_for():
    index = items.kiid_index(items.pin_map(INSTANCES))
    assert index == {"pin-r-1": frozenset({("R1", "1"), ("R101", "1")}),
                     "pin-r-2": frozenset({("R1", "2"), ("R101", "2")}),
                     "pin-pwr": frozenset({("#PWR01", "1")})}


def test_netlist_gives_each_instance_of_a_reused_sheet_its_own_reference():
    """The bug: keyed by KIID alone, R101 (the last instance read) took the
    pins of R1 too, so /CH1/OUT listed R101.2 and GND listed R101.1 twice."""
    api = _api(INSTANCES, CHANNEL_NETS)
    assert api.netlist() == {"CH1/OUT": [["R1", "2"]], "CH2/OUT": [["R101", "2"]],
                             "GND": [["#PWR01", "1"], ["R1", "1"], ["R101", "1"]]}
    assert api.netlist_warnings == []


def test_a_kiid_naming_one_pin_needs_no_matching_sheet_path():
    """A KiCad that reports no (or another) sheet path for a net still gets a
    pin whose KIID is unique in the whole schematic."""
    flat = [_inst("R1", ROOT, "sym-1", [("k1", "1"), ("k2", "2")]),
            _inst("C1", ROOT, "sym-2", [("k3", "1"), ("k4", "2")])]
    nets = [_net("/N1", _sheet((), ["k2", "k3", "k3"])),       # the KIID listed twice: once
            _net("N2", _sheet(("elsewhere",), ["k1"]))]
    api = _api(flat, nets)
    assert api.netlist() == {"N1": [["C1", "1"], ["R1", "2"]], "N2": [["R1", "1"]]}
    assert api.netlist_warnings == []


def test_an_ambiguous_kiid_on_an_unknown_sheet_is_reported_never_guessed():
    nets = [_net("/CH9/OUT", _sheet((ROOT_ID, "sheet-z"), ["pin-r-2"])),
            _net("GND", _sheet(SHEET_A, ["pin-r-1"]))]
    api = _api(INSTANCES, nets)
    assert api.netlist() == {"CH9/OUT": [], "GND": [["R1", "1"]]}
    [warning] = api.netlist_warnings
    assert "CH9/OUT" in warning and "pin-r-2" in warning and "/root-uuid/sheet-z" in warning
    assert "R1.2" in warning and "R101.2" in warning
    api._sch.nets = CHANNEL_NETS                        # a clean read clears the warnings
    api.netlist()
    assert api.netlist_warnings == []


def test_the_schematic_netlist_tool_returns_the_per_instance_netlist(monkeypatch):
    """The MCP tool reads SchematicApi.netlist() and its netlist_warnings."""
    api = _api(INSTANCES, CHANNEL_NETS[:2])
    path = "C:/fake/multi.kicad_sch"
    monkeypatch.setitem(kicad_server._SESSIONS, kicad_server._key(path),
                        kicad_server.Session(path, "api", api))
    res = kicad_server.schematic_netlist(path)
    assert res["ok"] and res["backend"] == "api" and "warnings" not in res
    assert res["nets"] == {"CH1/OUT": [["R1", "2"]], "CH2/OUT": [["R101", "2"]]}
    api._sch.nets = [_net("X", _sheet(("nowhere",), ["pin-r-1"]))]
    res = kicad_server.schematic_netlist(path)
    assert res["nets"] == {"X": []} and len(res["warnings"]) == 1


def test_an_edit_keeps_the_other_instances_of_a_reused_sheet_cached():
    """Instances of a reused sheet share their symbol KIID: replacing the
    edited one by KIID alone dropped its twins from the cache (R101 vanished)."""
    api = _api(INSTANCES, [])
    assert api.set_fields("R1", value="1k")["units"] == [1]
    assert [p["number"] for p in api.pin_info("R101")] == ["1", "2"]
    cached = sorted((i.reference, i.value) for i in api._instances())
    assert cached == [("#PWR01", "10k"), ("R1", "1k"), ("R101", "10k")]
    with pytest.raises(PinNotFoundError, match="no symbol 'R9'"):
        api.pin_info("R9")


# ============================================================================
# live tier: demos/multichannel on the KiCad 11 nightly (10.99) api-server
# ============================================================================
@pytest.fixture(scope="module")
def nightly():
    """The 10.99 install, probed on first use (collection runs no kicad-cli)."""
    reason = SchematicApi.unavailable_reason()
    if reason:
        pytest.skip(reason)
    import kicad_env as env
    return env.pick_api()


def _multichannel_copy(root: Path, tmp_path: Path) -> Path:
    src = root / "share" / "kicad" / "demos" / "multichannel"
    if not (src / "multichannel_mixer.kicad_sch").is_file():
        pytest.skip(f"demo multichannel is not installed under {root}")
    dst = tmp_path / "multichannel"            # a scratch copy: kicad-cli writes .kicad_prl
    shutil.copytree(src, dst)
    return dst / "multichannel_mixer.kicad_sch"


def _pad_nets(nets: dict[str, list[list[str]]]) -> dict[str, set[tuple[str, str]]]:
    """SchematicApi.netlist() as pad nets: power/flag symbols (#...) are not parts."""
    out = {name: {(ref, pin) for ref, pin in pins if not ref.startswith("#")}
           for name, pins in nets.items()}
    return {name: pads for name, pads in out.items() if pads}


def test_live_multichannel_netlist_equals_the_kicad_cli_export(nightly, tmp_path):
    import net_equivalence as ne
    sch = _multichannel_copy(nightly.root, tmp_path)
    xml = ne.export_schematic_xml(sch, tmp_path / "multichannel.xml", nightly.kicad_cli)
    expected = ne.xml_pad_nets(xml.read_text(encoding="utf-8"))
    with SchematicApi.open(sch, mode="headless") as api:
        nets = api.netlist()
        warnings = list(api.netlist_warnings)
    assert warnings == []
    res = ne.compare(expected, _pad_nets(nets), ignore_single_pin=False)   # every pad, too
    assert res.equal, res.summary() + "\n" + str(res.as_dict())[:3000]
    assert res.details["common_pads"] > 250 and res.details["nets_a"] > 70
    assert len(res.details["net_name_map"]) == res.details["nets_a"]
    # each channel's copy of the strip keeps its own references (4 x 36 parts)
    refs = {ref for pins in nets.values() for ref, _pin in pins if not ref.startswith("#")}
    assert {"IC1", "IC2", "IC3", "IC4", "IC5"} <= refs and len(refs) > 100
