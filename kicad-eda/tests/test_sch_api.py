"""backends.sch_api — the KiCad 11 IPC-API schematic backend.

Two tiers in one module:

* pure tests (geometry, mode selection, the power-flag rule, design
  validation) that run anywhere;
* live tests against the KiCad 11 nightly's headless ``kicad-cli api-server``,
  skipped when ``SchematicApi.available()`` is False. They share ONE headless
  session per module (the ~6 s library load happens once) and every document
  is closed in fixture teardown, so no kicad-cli.exe outlives the run.

The rotation/mirror oracle asks KiCad itself: a wire from the position
``pin_xy`` predicts to a label must put exactly that pin in the label's net.
"""
from __future__ import annotations

import csv
import io
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

import kicad_env as env
import toolchain as tc
from backends import kipy_bridge as kb
from backends import sch_geometry as geo
from backends import sch_items as items
from backends import sch_session as sess
from backends.errors import PinNotFoundError, SchematicApiError
from backends.sch_api import SchematicApi, build_from_netlist
from backends.sch_erc import run_erc, summarize
from backends.sch_netlist_build import power_flag_nets, validate_design
from conftest import load_example

requires_api = pytest.mark.skipif(not SchematicApi.available(),
                                  reason=SchematicApi.unavailable_reason() or "")
requires_kipy = pytest.mark.skipif(bool(kb.IMPORT_ERROR), reason=kb.IMPORT_ERROR or "")
ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


# ============================================================================
# pure tier
# ============================================================================
@pytest.mark.parametrize("deg,expected", [(0, (1, 2)), (90, (2, -1)), (180, (-1, -2)), (270, (-2, 1))])
def test_rotate_offset_table(deg, expected):
    assert geo.rotate_offset(1, 2, deg) == expected


def test_transform_offset_mirrors_after_rotation():
    # rotate 90 -> (2, -1); mirror_x flips y; mirror_y flips x
    assert geo.transform_offset(1, 2, 90, mirror_x=True) == (2, 1)
    assert geo.transform_offset(1, 2, 90, mirror_y=True) == (-2, -1)
    assert geo.transform_offset(1, 2, 0, mirror_x=True, mirror_y=True) == (-1, -2)


def test_pin_world_nm_is_exact_integer_arithmetic():
    assert geo.pin_world_nm((50_800_000, 50_800_000), (0, 3_810_000), 90) == (54_610_000, 50_800_000)


@pytest.mark.parametrize("bad", [45, "x", float("nan"), 30.5])
def test_orientation_deg_rejects_non_quadrant(bad):
    with pytest.raises(ValueError):
        geo.orientation_deg(bad)


def test_orientation_deg_normalises():
    assert geo.orientation_deg(-90) == 270
    assert geo.orientation_deg(450.0) == 90


def test_snap_and_route():
    assert geo.snap(3.0) == 2.54
    assert geo.snap(54.61) == 54.61
    assert geo.route((0, 0), (0, 0)) == []
    assert geo.route((0, 0), (5.08, 0)) == [(0.0, 0.0, 5.08, 0.0)]
    assert geo.route((0, 0), (5.08, 2.54)) == [(0.0, 0.0, 5.08, 0.0), (5.08, 0.0, 5.08, 2.54)]


def test_stub_continues_away_from_the_body():
    assert geo.stub_from((10.16, 0), (7.62, -2.54)) == (15.24, 0.0)
    assert geo.stub_from((0, 10.16), (0, 3.81)) == (0.0, 15.24)


def test_grid_cells_match_sch_build_layout():
    assert geo.grid_cell(0) == (50.8, 38.1)
    assert geo.grid_cell(4) == (304.8, 38.1)
    assert geo.grid_cell(5) == (50.8, 88.9)
    assert geo.flag_position(1) == (363.22, 53.34)


def test_select_mode_decisions(tmp_path):
    path = tmp_path / "a.kicad_sch"
    here = [sess.norm_path(path)]
    assert sess.select_mode("headless", path, here) == "headless"
    assert sess.select_mode("auto", path, None) == "headless"
    assert sess.select_mode("auto", path, [sess.norm_path(tmp_path / "b.kicad_sch")]) == "headless"
    assert sess.select_mode("auto", path, here) == "gui"
    assert sess.select_mode("gui", path, here) == "gui"
    with pytest.raises(SchematicApiError, match="no KiCad GUI"):
        sess.select_mode("gui", path, None)
    with pytest.raises(SchematicApiError, match="not open in the running KiCad"):
        sess.select_mode("gui", path, [])
    with pytest.raises(SchematicApiError, match="mode must be one of"):
        sess.select_mode("remote", path, None)


@requires_kipy
def test_connect_auto_falls_back_when_no_gui_answers(tmp_path, monkeypatch):
    """kipy dials lazily, so KiCad() succeeds even when nothing listens: the
    GUI probe must verify the connection, and 'auto' must then go headless
    instead of propagating the connection error."""
    class DeadKiCad:
        closed = False

        def __init__(self, **_kw):
            DeadKiCad.closed = False

        def get_version(self):
            raise kb.KiCadConnectionError("Failed to connect to KiCad: Connection refused")

        def close(self):
            DeadKiCad.closed = True

    headless = SimpleNamespace(mode="headless")
    monkeypatch.setattr(kb, "KiCad", DeadKiCad)
    monkeypatch.setattr(sess.KiCadSession, "headless", classmethod(lambda cls, **_kw: headless))
    assert sess.KiCadSession.gui() is None and DeadKiCad.closed is True
    assert sess.connect("auto", tmp_path / "a.kicad_sch") is headless
    with pytest.raises(SchematicApiError, match="no KiCad GUI answers"):
        sess.connect("gui", tmp_path / "a.kicad_sch")


def test_doc_sch_path_from_project_specifier(tmp_path):
    doc = SimpleNamespace(project=SimpleNamespace(path=str(tmp_path) + "\\", name="board"))
    assert sess.doc_sch_path(doc) == str(tmp_path / "board.kicad_sch")
    assert sess.doc_sch_path(SimpleNamespace(project=SimpleNamespace(path="", name="x"))) == ""
    assert sess.doc_sch_path(SimpleNamespace()) == ""


@requires_kipy
def test_open_schematic_paths_with_fake_kicad(tmp_path):
    docs = [SimpleNamespace(project=SimpleNamespace(path=str(tmp_path), name="one")),
            SimpleNamespace(project=SimpleNamespace(path="", name=""))]
    fake = SimpleNamespace(get_open_documents=lambda _t: docs)
    assert sess.open_schematic_paths(fake) == [sess.norm_path(tmp_path / "one.kicad_sch")]

    def refuse(_t):
        raise kb.ApiError("DOCTYPE_SCHEMATIC not supported")
    assert sess.open_schematic_paths(SimpleNamespace(get_open_documents=refuse)) == []


def _fake_pin_type(table: dict) -> callable:
    return lambda ref, pad: table.get((ref, str(pad)), "passive")


def test_power_flag_rule_matches_sch_build():
    design = load_example("lm358_buffer")
    types = {("U1", "8"): "power_in", ("U1", "4"): "power_in", ("U1", "1"): "output",
             ("U1", "7"): "output", ("U1", "3"): "input", ("U1", "2"): "input"}
    assert power_flag_nets(design, _fake_pin_type(types)) == {"P5V", "GND"}


def test_power_flag_rule_declared_rails_and_drivers():
    design = {"nets": {"VOUT": [["U1", "2"], ["C1", "1"]], "VDRV": [["U2", "1"], ["R1", "1"]],
                       "GHOST": [["R1", "2"]]},
              "net_specs": {"VOUT": {"voltage": 5.0}, "GHOST": {"current": 1.0}},
              "power_flags": ["VDRV", "NOT_A_NET"]}
    types = {("U2", "1"): "power_out"}
    assert power_flag_nets(design, _fake_pin_type(types)) == {"VOUT", "GHOST"}


@pytest.mark.parametrize("design,match", [
    ([], "must be a dict"),
    ({"components": []}, "non-empty list"),
    ({"components": [{"ref": "", "lib_id": "Device:R"}]}, "non-empty string 'ref'"),
    ({"components": [{"ref": "R1", "lib_id": "R"}]}, "lib_id"),
    ({"components": [{"ref": "R1", "lib_id": "Device:R"}] * 2}, "duplicate reference"),
    ({"components": [{"ref": "R1", "lib_id": "Device:R"}], "nets": []}, "must be a dict"),
    ({"components": [{"ref": "R1", "lib_id": "Device:R"}], "nets": {"N": [["R1"]]}}, "pairs"),
])
def test_validate_design_rejects_bad_shapes(design, match):
    with pytest.raises(SchematicApiError, match=match):
        validate_design(design)


@requires_kipy
def test_prime_definition_fills_the_missing_description_only():
    inst = kb.st.SchematicSymbolInstance()
    inst.description_field.text.value = "Resistor"
    items.prime_definition(inst)
    field = inst.definition.description_field
    assert (field.name, field.text.value, field.visible, field.allow_auto_place) == \
        ("Description", "Resistor", False, True)
    # KiCad reporting the field itself: left exactly as reported
    reported = kb.st.SchematicSymbolInstance()
    reported.description_field.text.value = "Resistor"
    reported.definition.description_field.text.value = "Library says"
    reported.definition.description_field.visible = True
    items.prime_definition(reported)
    assert reported.definition.description_field.text.value == "Library says"
    assert reported.definition.description_field.visible is True


def test_erc_summary_counts_errors_only():
    report = {"sheets": [{"violations": [{"type": "pin_not_connected", "severity": "error"},
                                         {"type": "pin_not_connected", "severity": "error"},
                                         {"type": "endpoint_off_grid", "severity": "warning"}]}]}
    assert summarize(report) == {"ok": False, "violations": 2,
                                 "types": {"pin_not_connected": 2},
                                 "by_severity": {"error": 2, "warning": 1}}
    assert summarize({"sheets": []})["ok"] is True


# ============================================================================
# live tier — one headless KiCad 11 session for the module
# ============================================================================
@pytest.fixture(scope="module")
def session():
    if not SchematicApi.available():
        pytest.skip(SchematicApi.unavailable_reason() or "API backend unavailable")
    live = sess.KiCadSession.headless()
    live.load_libraries()
    yield live
    live.close()


@pytest.fixture
def new_sch(session, tmp_path):
    """Factory: a fresh, empty schematic on the shared session; every document
    it opened is closed after the test."""
    opened: list[SchematicApi] = []

    def make(name: str = "t") -> SchematicApi:
        api = SchematicApi.open(tmp_path / f"{name}.kicad_sch", create=True, overwrite=True,
                                session=session)
        opened.append(api)
        return api

    yield make
    for api in opened:
        api.discard()


def _lib_symbol_findings(sch: Path) -> dict:
    """{type: count} of ERC's lib_symbol_* findings at EVERY severity (the
    embedded-symbol checks are warnings, which --severity-error would hide)."""
    res = tc.erc_json(str(sch), str(sch.with_suffix(".allsev.json")), severity_error=False)
    assert "erc_error" not in res, res
    types = Counter(v["type"] for s in res["report"]["sheets"] for v in s["violations"])
    return {t: n for t, n in types.items() if t.startswith("lib_symbol")}


def _instance_lib_ids(sch: Path) -> list[str]:
    text = sch.read_text(encoding="utf-8")
    return re.findall(r'\(lib_id "([^"]*)"', text.split("\n\t(symbol", 1)[1])


@requires_api
def test_field_updates_keep_embedded_symbols_in_sync_with_the_library(new_sch):
    api = new_sch("fields")
    placed = api.place_symbol("Device:R", "R1", 50.8, 50.8, value="10k",
                              footprint="Resistor_SMD:R_0603_1608Metric")
    assert (placed["value"], placed["footprint"]) == ("10k", "Resistor_SMD:R_0603_1608Metric")
    api.place_symbol("Device:R", "R2", 76.2, 50.8, value="1k")
    assert api.pending_fields == 2                            # deferred, not yet in KiCad
    for unit, x in ((1, 50.8), (2, 114.3), (3, 177.8)):
        api.place_symbol("Amplifier_Operational:LM358", "U1", x, 101.6, unit=unit)
    res = api.set_fields("U1", value="LM358", footprint="Package_SO:SOIC-8_3.9x4.9mm_P1.27mm")
    assert res["units"] == [1, 2, 3] and api.pending_fields == 0
    with pytest.raises(PinNotFoundError, match="unit 4 is not placed"):
        api.set_fields("U1", value="x", unit=4)
    with pytest.raises(SchematicApiError, match="give a value"):
        api.set_fields("U1")
    api.add_power_flag("GND", 50.8, 139.7)                    # the _move path
    api.save()
    values = {(s["ref"], s["unit"]): (s["value"], s["footprint"]) for s in api.symbols()}
    assert values[("U1", 2)] == ("LM358", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm")
    assert values[("R2", 1)] == ("1k", "")
    assert _lib_symbol_findings(Path(api.path)) == {}
    lib_ids = _instance_lib_ids(Path(api.path))
    assert len(lib_ids) == 6 and all(":" in lib_id for lib_id in lib_ids), lib_ids


@requires_api
def test_reads_never_flush_and_save_resyncs_the_library_link(new_sch):
    """The interactive tool sequence add_component -> list_symbols /
    schematic_netlist -> add_component(same lib_id) -> save, then more of the
    same after the save: every instance keeps its Library:Name and ERC finds
    no lib_symbol_* issue at any severity."""
    api = new_sch("interactive")
    api.place_symbol("Device:R", "R1", 50.8, 50.8, value="10k")
    assert {s["ref"]: s["value"] for s in api.symbols()}["R1"] == "10k"   # overlaid ...
    assert api.pending_fields == 1                                        # ... not pushed
    api.netlist()
    api.labels()
    assert api.pending_fields == 1
    api.place_symbol("Device:R", "R2", 76.2, 50.8, value="1k")
    api.place_symbol("Device:R", "R3", 101.6, 50.8)
    api.symbols()
    api.place_symbol("Amplifier_Operational:LM358", "U1", 50.8, 101.6, unit=1, value="LM358")
    api.symbols()
    api.place_symbol("Amplifier_Operational:LM358", "U1", 114.3, 101.6, unit=2)
    api.save()
    assert api.pending_fields == 0 and not api.is_modified()
    # save, then keep editing: the reload after save keeps later placements linked
    api.place_symbol("Device:R", "R4", 127.0, 50.8, value="4k7")
    api.place_symbol("Amplifier_Operational:LM358", "U1", 177.8, 101.6, unit=3)
    api.save()
    values = {(s["ref"], s["unit"]): s["value"] for s in api.symbols()}
    assert (values[("R1", 1)], values[("R4", 1)], values[("U1", 1)]) == ("10k", "4k7", "LM358")
    lib_ids = _instance_lib_ids(Path(api.path))
    assert len(lib_ids) == 7 and all(":" in lib_id for lib_id in lib_ids), lib_ids
    assert _lib_symbol_findings(Path(api.path)) == {}


@requires_api
def test_build_keeps_every_unit_linked_to_its_library(session, tmp_path):
    out = tmp_path / "lm358_links.kicad_sch"
    assert build_from_netlist(load_example("lm358_buffer"), str(out), session=session)["ok"]
    lib_ids = _instance_lib_ids(out)                          # 3 LM358 units, 2x Device:C, ...
    assert lib_ids and all(":" in lib_id for lib_id in lib_ids), lib_ids
    assert _lib_symbol_findings(out) == {}


@requires_api
def test_available_reports_the_nightly(session):
    assert session.mode == "headless"
    assert env.parse_version(session.kicad_version) >= (10, 99, 0)
    assert session.library_errors == []


@requires_api
def test_create_place_wire_power_save_erc(new_sch):
    api = new_sch("rc")
    r1 = api.place_symbol("Device:R", "R1", 50.8, 50.8, value="10k",
                          footprint="Resistor_SMD:R_0603_1608Metric")
    assert r1["ref"] == "R1" and r1["lib_id"] == "Device:R" and r1["at"] == [50.8, 50.8, 0]
    assert {p["number"]: (p["x_mm"], p["y_mm"]) for p in r1["pins"]} == \
        {"1": (50.8, 46.99), "2": (50.8, 54.61)}
    c1 = api.place_symbol("Device:C", "C1", 76.2, 50.8, rot=90)
    assert c1["at"][2] == 90
    wired = api.wire_pins("R1", "2", "C1", "1")
    assert len(wired["wires"]) == 2                     # not aligned -> L route
    assert wired["from"]["x_mm"] == 50.8 and wired["to"]["ref"] == "C1"
    nets = api.netlist()
    wire_net = next(pins for pins in nets.values() if ["R1", "2"] in pins)
    assert wire_net == [["C1", "1"], ["R1", "2"]]

    assert api.power_pin("R1", "1", "+3V3")["kind"] == "power_symbol"
    gnd = api.power_pin("C1", "2", "GND")
    assert gnd["lib_id"] == "power:GND" and gnd["ref"] == "#PWR02"
    fallback = api.power_pin("R1", "2", "NO_SUCH_RAIL_XYZ")
    assert fallback["kind"] == "global"                  # no power symbol -> global label
    nets = api.netlist()
    assert nets["GND"] == [["#PWR02", "1"], ["C1", "2"]]
    assert nets["+3V3"] == [["#PWR01", "1"], ["R1", "1"]]
    assert nets["NO_SUCH_RAIL_XYZ"] == [["C1", "1"], ["R1", "2"]]

    symbols = {s["ref"]: s for s in api.symbols()}
    assert symbols["R1"]["value"] == "10k"
    assert symbols["R1"]["footprint"] == "Resistor_SMD:R_0603_1608Metric"
    assert symbols["C1"]["at"] == [76.2, 50.8, 90]
    assert [lb["net"] for lb in api.labels()] == ["NO_SUCH_RAIL_XYZ"]

    assert api.is_modified()
    assert Path(api.save()).is_file() and not api.is_modified()
    erc = api.erc()
    assert erc["types"] == {"power_pin_not_driven": 2}   # the two rails have no driver
    api.add_power_flag("+3V3", 101.6, 50.8)
    api.add_power_flag("GND", 101.6, 76.2)
    erc = api.erc()
    assert erc["ok"] and erc["violations"] == 0, erc["types"]
    svgs = api.render_svg(Path(api.path).parent / "svg")
    assert svgs and all(Path(p).suffix == ".svg" for p in svgs)


@requires_api
def test_rotation_oracle_pin_positions(new_sch):
    """KiCad's own netlist confirms pin_xy for every rotation: a wire from the
    predicted R.2 position to a label puts exactly R.2 in that net."""
    api = new_sch("rot")
    for index, rot in enumerate(geo.ROTATIONS):
        ref = f"R{index + 1}"
        placed = api.place_symbol("Device:R", ref, 50.8 + 25.4 * index, 50.8, rot=rot)
        assert placed["at"][2] == rot
        x, y = api.pin_xy(ref, "2")
        far = geo.stub_from((x, y), (x - placed["at"][0], y - placed["at"][1]))
        api.add_wire(x, y, *far)
        api.add_label(f"NET_{rot}", *far)
    nets = api.netlist()
    for index, rot in enumerate(geo.ROTATIONS):
        assert nets[f"NET_{rot}"] == [[f"R{index + 1}", "2"]], (rot, nets)


@requires_api
def test_mirror_oracle_pin_positions(new_sch):
    """Mirrored instances (set through the API transform, as the GUI would)
    resolve too: LM358 unit-1 pins 3 and 1 are asymmetric in x and y."""
    api = new_sch("mirror")
    cases = [(rot, mirror) for rot in geo.ROTATIONS for mirror in ("mirror_x", "mirror_y")]
    for index, (rot, mirror) in enumerate(cases):
        ref = f"U{index + 1}"
        api.place_symbol("Amplifier_Operational:LM358", ref, 50.8 + 38.1 * (index % 4),
                         50.8 + 38.1 * (index // 4), rot=rot)
        inst = next(i for i in api.session.schematic_for(api.path).get_symbols()
                    if i.reference == ref)
        transform = inst.transform
        setattr(transform, mirror, True)
        inst.transform = transform
        api.session.schematic_for(api.path).update_items(inst)
    api.symbols()                                        # re-read the normalised transforms
    for index, (rot, mirror) in enumerate(cases):
        ref = f"U{index + 1}"
        at = next(s["at"] for s in api.symbols() if s["ref"] == ref)
        for pin in ("3", "1"):
            x, y = api.pin_xy(ref, pin)
            far = geo.stub_from((x, y), (x - at[0], y - at[1]))
            api.add_wire(x, y, *far)
            api.add_label(f"N_{ref}_{pin}", *far)
    nets = api.netlist()
    for index, _case in enumerate(cases):
        ref = f"U{index + 1}"
        assert nets[f"N_{ref}_3"] == [[ref, "3"]], (ref, nets[f"N_{ref}_3"])
        assert nets[f"N_{ref}_1"] == [[ref, "1"]], (ref, nets[f"N_{ref}_1"])


@requires_api
def test_pin_lookup_errors_are_specific(new_sch):
    api = new_sch("errors")
    api.place_symbol("Amplifier_Operational:LM358", "U1", 50.8, 50.8, unit=1)
    assert api.pin_xy("U1", "3") == (43.18, 48.26)
    assert api.pin_xy("U1", "+") == (43.18, 48.26)         # by name
    with pytest.raises(PinNotFoundError, match="unit \\[3\\] which is not placed"):
        api.pin_xy("U1", "8")
    with pytest.raises(PinNotFoundError, match="no pin '99'"):
        api.pin_xy("U1", "99")
    with pytest.raises(PinNotFoundError, match="no symbol 'Q9'"):
        api.pin_xy("Q9", "1")
    with pytest.raises(SchematicApiError, match="Library:Symbol"):
        api.place_symbol("Device", "R1", 0, 0)
    with pytest.raises(SchematicApiError, match="rotation must be one of"):
        api.place_symbol("Device:R", "R1", 0, 0, rot=45)
    with pytest.raises(SchematicApiError, match="not found"):
        api.place_symbol("Device:NoSuchSymbolXYZ", "R1", 0, 0)
    assert {info["number"]: info["unit"] for info in api.pin_info("U1")}["8"] == 3


@requires_api
def test_open_refuses_missing_and_existing_files(session, tmp_path):
    with pytest.raises(SchematicApiError, match="create=True"):
        SchematicApi.open(tmp_path / "missing.kicad_sch", session=session)
    with pytest.raises(SchematicApiError, match="must end with"):
        SchematicApi.open(tmp_path / "x.txt", create=True, session=session)
    api = SchematicApi.open(tmp_path / "e.kicad_sch", create=True, session=session)
    api.save()
    api.close()
    with pytest.raises(SchematicApiError, match="overwrite=True"):
        SchematicApi.open(tmp_path / "e.kicad_sch", create=True, session=session)


@requires_api
@pytest.mark.parametrize("name", ["lm358_buffer", "cp2102n_uart"])
def test_build_from_netlist_is_erc_clean(session, tmp_path, name):
    design = load_example(name)
    result = build_from_netlist(design, str(tmp_path / f"{name}.kicad_sch"), session=session)
    assert result["ok"], result
    assert result["backend"] == "api" and result["missing_pins"] == []
    assert result["components"] == len(design["components"])
    assert result["nets"] == len(design["nets"])
    net_pins = sum(len(conns) for conns in design["nets"].values())
    assert result["labels"] == net_pins
    assert result["no_connects"] == len(design.get("no_connect", []))
    erc = run_erc(result["sch"])
    assert erc["violations"] == 0, erc["types"]
    with SchematicApi.open(result["sch"], session=session) as api:
        nets = api.netlist()
        for net, conns in design["nets"].items():
            assert {tuple(c) for c in conns} <= {tuple(p) for p in nets[net]}, net
        placed = [s["ref"] for s in api.symbols() if not s["ref"].startswith("#")]
        assert set(placed) == {c["ref"] for c in design["components"]}
        assert len(placed) == result["placements"]


@requires_api
def test_build_lm358_layout_matches_sch_build(session, tmp_path):
    design = load_example("lm358_buffer")
    result = build_from_netlist(design, str(tmp_path / "lm.kicad_sch"), session=session)
    assert result["ok"] and result["power_flags"] == ["GND", "P5V"]
    with SchematicApi.open(result["sch"], session=session) as api:
        cells = [(s["ref"], s["unit"], s["at"]) for s in api.symbols() if not s["ref"].startswith("#")]
    # sch_build order: U1 units 1,2,3 then J1, C1, C2 — one 63.5 x 50.8 cell each
    assert sorted(cells, key=lambda c: (c[2][1], c[2][0])) == [
        ("U1", 1, [50.8, 38.1, 0]), ("U1", 2, [114.3, 38.1, 0]), ("U1", 3, [177.8, 38.1, 0]),
        ("J1", 1, [241.3, 38.1, 0]), ("C1", 1, [304.8, 38.1, 0]), ("C2", 1, [50.8, 88.9, 0])]
    with SchematicApi.open(result["sch"], session=session) as api:
        values = {(s["ref"], s["unit"]): (s["value"], s["footprint"]) for s in api.symbols()}
    assert values[("U1", 2)] == values[("U1", 1)] == ("LM358", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm")


@requires_api
def test_build_with_bogus_pad_writes_nothing(session, tmp_path):
    design = load_example("lm358_buffer")
    design["nets"]["BOGUS"] = [["U1", "99"], ["Q9", "1"]]
    out = tmp_path / "bogus.kicad_sch"
    result = build_from_netlist(design, str(out), session=session)
    assert result["ok"] is False and result["backend"] == "api"
    assert result["missing_pins"] == [["U1", "99"], ["Q9", "1"]]
    assert not out.exists()
    # the shared session is still usable afterwards
    again = build_from_netlist(load_example("lm358_buffer"), str(tmp_path / "ok.kicad_sch"),
                               session=session)
    assert again["ok"]


@requires_api
def test_open_sch_build_file_and_read_it(session, tmp_path):
    """A KiCad-9/10-format file written by the S-expression fallback round-trips."""
    import sch_build
    symbols_dir = env.symbols_dir(env.pick_cli())
    assert symbols_dir is not None
    netlist = EXAMPLES / "lm358_buffer" / "netlist.json"
    out = tmp_path / "legacy.kicad_sch"
    built = sch_build.build(str(netlist), str(out), str(symbols_dir))
    assert built["ok"], built
    design = json.loads(netlist.read_text(encoding="utf-8"))
    with SchematicApi.open(out, session=session) as api:
        assert api.mode == "headless" and not api.is_modified()
        refs = sorted({s["ref"] for s in api.symbols() if not s["ref"].startswith("#")})
        assert refs == sorted(c["ref"] for c in design["components"])
        nets = api.netlist()
        for net, conns in design["nets"].items():
            assert {tuple(c) for c in conns} <= {tuple(p) for p in nets[net]}, net
        assert len(api.labels()) == 18 + 2                 # a label per net pin + 2 flags


def _kicad_cli_pids() -> set[int] | None:
    """PIDs of every kicad-cli.exe (Windows); None where tasklist is absent."""
    if sys.platform != "win32":
        return None
    proc = subprocess.run(["tasklist", "/FI", "IMAGENAME eq kicad-cli.exe", "/FO", "CSV", "/NH"],
                          capture_output=True, text=True, timeout=30)
    return {int(row[1]) for row in csv.reader(io.StringIO(proc.stdout))
            if len(row) > 1 and row[1].isdigit()}


def _assert_no_server_left(started: set[int] | None) -> None:
    if started is not None:
        assert not (started & (_kicad_cli_pids() or set())), "kicad-cli api-server leaked"


@requires_api
def test_owned_session_is_stopped_on_close(tmp_path):
    before = _kicad_cli_pids()
    api = SchematicApi.open(tmp_path / "own.kicad_sch", create=True, mode="headless")
    live = api.session
    started = None if before is None else _kicad_cli_pids() - before
    assert started is None or len(started) == 1
    api.place_symbol("Device:R", "R1", 50.8, 50.8)
    api.close()
    assert live.closed
    _assert_no_server_left(started)
    with pytest.raises(SchematicApiError, match="is closed"):
        api.save()


@requires_api
def test_discard_then_close_stops_an_owned_server(tmp_path):
    before = _kicad_cli_pids()
    api = SchematicApi.open(tmp_path / "own2.kicad_sch", create=True, mode="headless")
    started = None if before is None else _kicad_cli_pids() - before
    api.place_symbol("Device:R", "R1", 50.8, 50.8, value="1k")
    api.discard()
    assert api.session.closed                     # discard alone releases what it owns
    api.close()                                   # idempotent afterwards
    _assert_no_server_left(started)
    with pytest.raises(SchematicApiError, match="is closed"):
        api.place_symbol("Device:R", "R2", 76.2, 50.8)


@requires_api
def test_open_auto_without_a_gui_launches_headless(tmp_path, monkeypatch):
    """No KiCad GUI on the socket -> 'auto' must not fail, it goes headless."""
    monkeypatch.setenv("KICAD_API_SOCKET", "ipc://" + str(tmp_path / "nobody-listens.sock"))
    assert sess.KiCadSession.gui() is None
    api = SchematicApi.open(tmp_path / "auto.kicad_sch", create=True, mode="auto")
    try:
        assert api.mode == "headless"
    finally:
        api.close()
    assert api.session.closed


@requires_api
def test_build_with_bogus_pad_on_an_owned_session_leaves_no_server(tmp_path):
    """build_from_netlist without session= (the kicad_server path) takes the
    discard() route on missing pins; its private api-server must still stop."""
    design = load_example("lm358_buffer")
    design["nets"]["BOGUS"] = [["U1", "99"]]
    before = _kicad_cli_pids()
    result = build_from_netlist(design, str(tmp_path / "bogus_owned.kicad_sch"))
    assert result["ok"] is False and result["missing_pins"] == [["U1", "99"]]
    if before is not None:
        assert _kicad_cli_pids() <= before, "kicad-cli api-server leaked"
