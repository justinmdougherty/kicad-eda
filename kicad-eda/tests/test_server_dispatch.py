"""kicad_server schematic dispatch: backend selection, the session registry,
the file-model fallback (backend "file" everywhere when the API is
unavailable), the API route against a fake backend, the open-document
guard revision, and the MCP tool module registry (TOOL_MODULES). No KiCad
needed: the IPC connection and the symbol library lookups are patched; pin
offsets come from sidecars written into tmp_path.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

import board_pipeline_tools
import board_route_tools
import import_tools
import kicad_env as env
import kicad_ipc as ipc
import kicad_server as k
import library_tools
import mechanical_tools
import pcb_ipc_tools
import review_tools
import sch_file_model as sfm
import tool_registry
import toolchain as tc

R_PINS = {"1": {"x": 0.0, "y": -3.81}, "2": {"x": 0.0, "y": 3.81}}
C_PINS = {"1": {"x": 0.0, "y": -3.81}, "2": {"x": 0.0, "y": 3.81}}
NO_GUI = {"ipc_connected": False, "reason": "test: no GUI"}


def _gui(docs: list[dict], kicad11: bool) -> dict:
    return {"ipc_connected": True, "kicad": object(), "kicad_version": "10.99.0" if kicad11 else "9.0.4",
            "open_documents": docs, "capabilities": ipc.capabilities((10, 99, 0) if kicad11 else (9, 0, 4))}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """No GUI, no symbol libraries, API unavailable, empty registry."""
    monkeypatch.setattr(ipc, "connect", lambda: NO_GUI)
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": None)
    monkeypatch.setattr(k, "_API", {"checked": True, "module": None, "reason": "test: no backend"})
    monkeypatch.setattr(k, "_api_available", lambda: False)
    monkeypatch.setattr(k, "_SESSIONS", {})
    pins = tmp_path / "pins"
    pins.mkdir()
    (pins / "Device_R.pins.json").write_text(json.dumps(R_PINS), encoding="utf-8")
    (pins / "Device_C.pins.json").write_text(json.dumps(C_PINS), encoding="utf-8")


@pytest.fixture
def sch(tmp_path) -> str:
    return str(tmp_path / "design.kicad_sch")


# ----------------------------------------------------------------------------
# File-model fallback
# ----------------------------------------------------------------------------
def test_create_falls_back_to_file_backend(sch):
    res = k.create_schematic(sch)
    assert res["ok"] and res["backend"] == "file" and res["mode"] == "file"
    assert k.server_status()["schematic_backend"] == "file"


def test_backend_api_insists_and_fails_loudly(sch):
    with pytest.raises(ValueError, match="test: no backend"):
        k.create_schematic(sch, backend="api")


def test_every_schematic_tool_reports_backend_file(sch):
    k.create_schematic(sch)
    calls = [
        k.add_component(sch, "R1", "Device:R", "10k", 100, 100),
        k.add_components_batch(sch, [{"ref": "C1", "lib_id": "Device:C", "value": "1u",
                                      "x": 120, "y": 100, "rot": 90}]),
        k.add_wire(sch, 0, 0, 10, 0),
        k.add_label(sch, "N1", 10, 0, rot=90),
        k.add_power_port(sch, "GND", 20, 0),
        k.wire_by_ref(sch, "R1", "2", "C1", "1"),
        k.wire_pins(sch, "R1", "1", "C1", "2"),
        k.label_pin(sch, "R1", "1", "IN"),
        k.power_pin(sch, "C1", "2", "GND"),
        k.no_connect_pin(sch, "R1", "2"),
        k.add_junction(sch, 10, 0),
        k.list_symbols(sch),
        k.list_nets(sch),
        k.schematic_netlist(sch),
        k.save_schematic(sch),
        k.close_schematic(sch),
    ]
    assert all(c["ok"] for c in calls), [c for c in calls if not c["ok"]]
    assert {c["backend"] for c in calls} == {"file"}


def test_pins_are_reported_from_the_sidecar_with_rotation(sch):
    k.create_schematic(sch)
    res = k.add_component(sch, "R1", "Device:R", "10k", 100, 100, rot=90)
    pins = {p["number"]: (p["x_mm"], p["y_mm"]) for p in res["pins"]}
    # rotation 90: offset (dx, dy) -> (dy, -dx); pin 1 at (0, -3.81) -> (96.19, 100)
    assert pins == {"1": (96.19, 100.0), "2": (103.81, 100.0)}


def test_wire_pins_routes_an_l_when_not_aligned(sch):
    k.create_schematic(sch)
    k.add_component(sch, "R1", "Device:R", "10k", 100, 100)
    k.add_component(sch, "C1", "Device:C", "1u", 130, 120)
    res = k.wire_by_ref(sch, "R1", "2", "C1", "1")
    assert res["wires"] == [[100.0, 103.81, 130.0, 103.81], [130.0, 103.81, 130.0, 116.19]]


def test_file_netlist_reads_connectivity_off_the_geometry(sch):
    k.create_schematic(sch)
    k.add_component(sch, "R1", "Device:R", "10k", 100, 100)
    k.add_component(sch, "C1", "Device:C", "1u", 130, 120)
    k.wire_by_ref(sch, "R1", "2", "C1", "1")
    k.label_pin(sch, "R1", "1", "IN")
    k.power_pin(sch, "C1", "2", "GND")
    nets = k.schematic_netlist(sch)["nets"]
    assert nets["IN"] == [["R1", "1"]]
    assert nets["GND"] == [["C1", "2"]]
    assert nets["Net-(C1-Pad1)"] == [["C1", "1"], ["R1", "2"]]
    assert k.list_nets(sch)["nets"] == ["GND", "IN", "Net-(C1-Pad1)"]


def test_save_writes_no_connect_and_junction(sch):
    k.create_schematic(sch)
    k.add_component(sch, "R1", "Device:R", "10k", 100, 100)
    k.no_connect_pin(sch, "R1", "1")
    k.add_junction(sch, 50, 50)
    res = k.save_schematic(sch)
    assert res["ok"] and res["no_connects"] == 1 and res["junctions"] == 1
    text = Path(sch).read_text(encoding="utf-8")
    assert "(no_connect (at 100.0000 96.1900)" in text
    assert "(junction (at 50.0000 50.0000)" in text
    assert "(sheet_instances" in text


def test_unknown_pin_is_an_ok_false_with_reason(sch):
    k.create_schematic(sch)
    k.add_component(sch, "R1", "Device:R", "10k", 100, 100)
    res = k.label_pin(sch, "R1", "9", "X")
    assert res["ok"] is False and res["backend"] == "file"
    assert "has no pin '9'" in res["reason"]


def test_batch_validates_before_placing_anything(sch):
    k.create_schematic(sch)
    with pytest.raises(ValueError, match="rot must be one of"):
        k.add_components_batch(sch, [{"ref": "R1", "lib_id": "Device:R", "x": 0, "y": 0},
                                     {"ref": "R2", "lib_id": "Device:R", "x": 0, "y": 0, "rot": 45}])
    assert k.list_symbols(sch)["symbols"] == []


def test_unregistered_path_is_an_error(sch):
    with pytest.raises(ValueError, match="No schematic loaded"):
        k.add_wire(sch, 0, 0, 1, 1)


def test_dirty_tracking_and_close_discard(sch):
    k.create_schematic(sch)
    sess = k._session(sch)
    assert sess.dirty is False
    k.add_wire(sch, 0, 0, 1, 0)
    assert sess.dirty is True
    k.save_schematic(sch)
    assert sess.dirty is False
    k.add_wire(sch, 0, 0, 2, 0)
    res = k.close_schematic(sch)
    assert res["discarded_changes"] is True and res["saved"] is False
    assert k.server_status()["schematics_open"] == []


def test_close_with_save_writes_first(sch):
    k.create_schematic(sch)
    k.add_wire(sch, 0, 0, 1, 0)
    res = k.close_schematic(sch, save=True)
    assert res["saved"] is True and res["discarded_changes"] is False
    assert Path(sch).is_file()


def test_load_schematic_file_backend_notes_empty_model(sch):
    Path(sch).write_text("(kicad_sch)", encoding="utf-8")
    res = k.load_schematic(sch)
    assert res["ok"] and res["backend"] == "file" and "EMPTY model" in res["note"]


# ----------------------------------------------------------------------------
# Open-document guard
# ----------------------------------------------------------------------------
def test_file_save_refused_when_open_in_a_kicad9_gui(sch, monkeypatch):
    k.create_schematic(sch)
    monkeypatch.setattr(ipc, "connect", lambda: _gui([{"type": "schematic", "path": sch}], False))
    res = k.save_schematic(sch)
    assert res["ok"] is False and "Eeschema" in res["reason"] and res["backend"] == "file"


def test_create_refused_over_an_open_document(sch, monkeypatch):
    monkeypatch.setattr(ipc, "connect", lambda: _gui([{"type": "schematic", "path": sch}], True))
    res = k.create_schematic(sch)
    assert res["ok"] is False and "load_schematic" in res["hint"]


# ----------------------------------------------------------------------------
# API route against a fake backend
# ----------------------------------------------------------------------------
class FakeApiError(Exception):
    pass


class FakeApi:
    """Records calls; mirrors the SchematicApi contract shapes."""
    opened: list["FakeApi"] = []

    def __init__(self, path: str, mode: str, create: bool):
        self.path, self.mode, self.create = path, mode, create
        self.kicad_version = "10.99.0"
        self.calls: list[tuple] = []
        self.closed = False
        FakeApi.opened.append(self)

    @classmethod
    def available(cls) -> bool:
        return True

    @classmethod
    def open(cls, path, create=False, overwrite=False, mode="auto"):
        if "boom" in str(path):
            raise FakeApiError("api-server failed to start: boom")
        return cls(str(path), "gui" if mode == "auto" and "gui" in str(path) else
                   ("headless" if mode in ("auto", "headless") else mode), create)

    def place_symbol(self, lib_id, ref, x_mm, y_mm, rot=0, unit=1, value="", footprint=""):
        self.calls.append(("place_symbol", lib_id, ref))
        if lib_id == "Nope:X":
            raise FakeApiError("symbol Nope:X not found")
        return {"ref": ref, "lib_id": lib_id, "unit": unit, "at": [x_mm, y_mm, rot],
                "pins": [{"number": "1", "name": "~", "x_mm": x_mm, "y_mm": y_mm - 3.81}]}

    def set_fields(self, ref, value="", footprint="", unit=None):
        self.calls.append(("set_fields", ref, value, footprint, unit))
        return {"ref": ref, "units": [unit or 1], "value": value, "footprint": footprint}

    def add_label(self, net, x_mm, y_mm):
        return {"net": net, "at": [x_mm, y_mm]}

    def symbols(self):
        return [{"ref": c[2], "lib_id": c[1], "value": "", "unit": 1, "at": [0, 0, 0]}
                for c in self.calls if c[0] == "place_symbol"]

    def netlist(self):
        return {"N1": [["R1", "1"]]}

    def save(self):
        self.calls.append(("save",))
        return self.path

    def render_svg(self, out_dir):
        return [str(Path(out_dir) / "design.svg")]

    def close(self):
        self.closed = True


class FakeModule:
    SchematicApi = FakeApi
    SchematicApiError = FakeApiError

    @staticmethod
    def build_from_netlist(design, out_path, overwrite=True):
        return {"ok": True, "sch": out_path, "components": len(design["components"]),
                "nets": len(design["nets"]), "missing_pins": [], "warnings": [], "backend": "api"}


@pytest.fixture
def fake_api(monkeypatch):
    FakeApi.opened = []
    monkeypatch.setattr(k, "_API", {"checked": True, "module": FakeModule, "reason": None})
    monkeypatch.setattr(k, "_api_available", lambda: True)
    return FakeApi


def test_api_route_dispatches_and_tags_backend_api(sch, fake_api):
    res = k.create_schematic(sch)
    assert res["backend"] == "api" and res["mode"] == "headless"
    placed = k.add_component(sch, "R1", "Device:R", "10k", 100, 100)
    assert placed["ok"] and placed["backend"] == "api" and placed["pins"][0]["number"] == "1"
    assert k.list_symbols(sch)["symbols"][0]["ref"] == "R1"
    assert k.schematic_netlist(sch)["nets"] == {"N1": [["R1", "1"]]}
    lbl = k.add_label(sch, "N1", 0, 0, rot=90)
    assert lbl["ok"] and lbl["note"] == "rot ignored by the API backend"
    saved = k.save_schematic(sch)
    assert saved["ok"] and saved["backend"] == "api" and saved["result"] == sch
    assert k.render_sch(sch)["files"][0].endswith("design.svg")
    assert k.server_status()["schematic_backend"] == "api"


def test_batch_on_the_api_places_every_entry_and_marks_the_session_dirty(sch, fake_api):
    k.create_schematic(sch)
    res = k.add_components_batch(sch, [
        {"ref": "U1", "lib_id": "Amp:Op", "value": "LM358", "x": 0, "y": 0, "unit": 1},
        {"ref": "U1", "lib_id": "Amp:Op", "value": "LM358", "x": 10, "y": 0, "unit": 2},
        {"ref": "R1", "lib_id": "Device:R", "x": 20, "y": 0}])
    assert res["ok"] and res["placed"] == ["U1", "U1", "R1"] and res["backend"] == "api"
    assert [c[0] for c in fake_api.opened[0].calls] == ["place_symbol"] * 3
    assert k._session(sch).dirty is True


def test_api_errors_become_ok_false_with_reason(sch, fake_api):
    k.create_schematic(sch)
    res = k.add_component(sch, "X1", "Nope:X", "", 0, 0)
    assert res["ok"] is False and res["backend"] == "api" and "not found" in res["reason"]
    boom = k.create_schematic(str(Path(sch).parent / "boom.kicad_sch"))
    assert boom["ok"] is False and "boom" in boom["reason"] and boom["backend"] == "api"


def test_headless_holds_one_document_at_a_time(sch, tmp_path, fake_api):
    other = str(tmp_path / "other.kicad_sch")
    k.create_schematic(sch)
    k.add_component(sch, "R1", "Device:R", "", 0, 0)          # dirty
    refused = k.create_schematic(other)
    assert refused["ok"] is False and "unsaved changes" in refused["reason"]
    k.save_schematic(sch)
    ok = k.create_schematic(other)
    assert ok["ok"] and fake_api.opened[0].closed is True     # idle session released ...
    assert ok["released_sessions"] == [sch]                   # ... and the response says so
    assert "load_schematic" in ok["released_note"]
    assert [s["path"] for s in k.server_status()["schematics_open"]] == [other]
    with pytest.raises(ValueError, match="No schematic loaded"):
        k.list_symbols(sch)


def test_release_close_errors_are_reported_not_dropped(sch, tmp_path, fake_api):
    def broken_close():
        raise FakeApiError("api-server did not stop")

    k.create_schematic(sch)
    fake_api.opened[0].close = broken_close
    other = str(tmp_path / "other.kicad_sch")
    res = k.create_schematic(other)
    assert res["ok"] and res["released_sessions"] == [sch]
    assert res["released_session_close_errors"] == [f"{sch}: FakeApiError: api-server did not stop"]
    fake_api.opened[1].close = broken_close
    again = k.create_schematic(other)                          # re-creating the same path
    assert again["ok"] and again["previous_session_close_error"] == \
        "FakeApiError: api-server did not stop"
    Path(other).write_text("(kicad_sch)", encoding="utf-8")
    fake_api.opened[2].close = broken_close
    loaded = k.load_schematic(other)
    assert loaded["ok"] and loaded["previous_session_close_error"] == \
        "FakeApiError: api-server did not stop"


def test_close_schematic_stops_the_api_session(sch, fake_api):
    k.create_schematic(sch)
    res = k.close_schematic(sch)
    assert res["ok"] and fake_api.opened[0].closed is True
    with pytest.raises(ValueError, match="No schematic loaded"):
        k.list_symbols(sch)


def test_load_connects_to_a_kicad11_gui_that_has_the_file_open(sch, fake_api, monkeypatch):
    gui_path = str(Path(sch).parent / "gui.kicad_sch")
    Path(gui_path).write_text("(kicad_sch)", encoding="utf-8")
    monkeypatch.setattr(ipc, "connect", lambda: _gui([{"type": "schematic", "path": gui_path}], True))
    res = k.load_schematic(gui_path)
    assert res["ok"] and res["backend"] == "api" and res["mode"] == "gui"
    assert k.save_schematic(gui_path)["ok"]                    # the GUI session may write


def test_load_refuses_a_file_open_in_a_kicad9_gui(sch, fake_api, monkeypatch):
    Path(sch).write_text("(kicad_sch)", encoding="utf-8")
    monkeypatch.setattr(ipc, "connect", lambda: _gui([{"type": "schematic", "path": sch}], False))
    res = k.load_schematic(sch)
    assert res["ok"] is False and "Eeschema" in res["reason"]


def test_build_from_netlist_uses_the_api_and_reports_backend(tmp_path, fake_api, monkeypatch):
    netlist = tmp_path / "netlist.json"
    netlist.write_text(json.dumps({"components": [{"ref": "R1", "lib_id": "Device:R"}],
                                   "nets": {"N1": [["R1", "1"]]}}), encoding="utf-8")
    res = k.build_schematic_from_netlist(str(netlist), run_erc=False)
    assert res["ok"] and res["backend"] == "api" and res["components"] == 1
    assert res["sch"] == str(netlist.with_suffix(".kicad_sch"))
    assert "released_sessions" not in res
    idle = str(tmp_path / "idle.kicad_sch")
    k.create_schematic(idle)                                   # an idle headless session
    res = k.build_schematic_from_netlist(str(netlist), run_erc=False)
    assert res["ok"] and res["released_sessions"] == [idle] and fake_api.opened[0].closed


def test_build_from_netlist_falls_back_to_sch_build(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_run(args, **kw):
        seen["args"] = args
        return type("R", (), {"returncode": 0, "stdout": '{"ok": true, "components": 1}',
                              "stderr": ""})()

    monkeypatch.setattr(tc, "run", fake_run)
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": str(tmp_path))
    netlist = tmp_path / "netlist.json"
    netlist.write_text("{}", encoding="utf-8")
    res = k.build_schematic_from_netlist(str(netlist), run_erc=False)
    assert res["ok"] and res["backend"] == "file"
    assert seen["args"][1].endswith("sch_build.py")


# ----------------------------------------------------------------------------
# Toolchain wrappers + status
# ----------------------------------------------------------------------------
def test_pipeline_facing_names_resolve_through_toolchain(monkeypatch):
    monkeypatch.setattr(tc, "kicad_cli", lambda: "N:/10.99/kicad-cli.exe")
    monkeypatch.setattr(tc, "board_cli", lambda: "S:/9.0/kicad-cli.exe")
    monkeypatch.setattr(tc, "kicad_python", lambda: "S:/9.0/python.exe")
    monkeypatch.setattr(tc, "kicad_share", lambda: Path("N:/share/kicad"))
    assert k.KICAD_CLI == "S:/9.0/kicad-cli.exe"       # the BOARD cli, not the newest
    assert k._find_kicad_cli() == "N:/10.99/kicad-cli.exe"
    assert k._kicad_python() == "S:/9.0/python.exe"
    assert k._kicad_share() == Path("N:/share/kicad")
    with pytest.raises(AttributeError):
        k.NOT_A_THING  # noqa: B018


def test_status_reports_both_clis_and_the_policy(monkeypatch):
    monkeypatch.setattr(tc, "kicad_cli", lambda: "N:/10.99/kicad-cli.exe")
    monkeypatch.setattr(tc, "board_cli", lambda: "S:/9.0/kicad-cli.exe")
    st = k.server_status()
    assert st["kicad_cli_schematic"] == "N:/10.99/kicad-cli.exe"
    assert st["kicad_cli_board"] == "S:/9.0/kicad-cli.exe"
    assert st["kicad_cli"] is True and st["kicad_cli_path"] == st["kicad_cli_schematic"]
    assert st["kicad_cli_policy"] == tc.KICAD_CLI_POLICY
    assert "board" in st["kicad_cli_policy"] and "newest" in st["kicad_cli_policy"].lower()


def test_status_reports_installs_and_backend(monkeypatch):
    monkeypatch.setattr(tc, "installs", lambda: [{"version": "9.0.4", "has_api_server": False}])
    st = k.server_status()
    assert st["installs"][0]["version"] == "9.0.4"
    assert st["schematic_backend"] == "file" and st["schematic_backend_reason"] == "test: no backend"
    conn = k.test_kicad_connection()
    assert conn["ipc_connected"] is False and conn["schematic_backend"] == "file"
    assert conn["installs"][0]["version"] == "9.0.4" and "api_kicad_version" in conn


def test_file_model_error_is_a_value_error():
    assert issubclass(sfm.FileModelError, ValueError)


# ----------------------------------------------------------------------------
# design_report tool: unreadable symbols are surfaced, never a clean report
# ----------------------------------------------------------------------------
def _write_netlist(tmp_path: Path, lib_id: str) -> Path:
    netlist = tmp_path / "netlist.json"
    netlist.write_text(json.dumps({
        "components": [{"ref": "U1", "lib_id": lib_id, "value": "X", "footprint": ""}],
        "nets": {"N1": [["U1", "1"]]}}), encoding="utf-8")
    return netlist


def test_design_report_reports_ok_false_when_a_symbol_cannot_be_read(tmp_path, monkeypatch):
    nope = tmp_path / "nope"                      # a symbols dir with no libraries
    nope.mkdir()
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": str(nope))
    res = k.design_report(str(_write_netlist(tmp_path, "Nope:Chip")))
    assert res["ok"] is False
    assert len(res["errors"]) == 1 and res["errors"][0].startswith("pin types unavailable for Nope:Chip (U1)")
    assert res["reason"].startswith("design review could not run for 1 symbol(s)")
    assert res["report"]["errors"] == res["errors"]
    assert Path(res["markdown_path"]).is_file() and "## Errors" in res["markdown"]


def test_design_report_is_ok_without_symbols(tmp_path):
    # symbols_dir None (autouse fixture): no pin-type checks and no errors
    res = k.design_report(str(_write_netlist(tmp_path, "Device:R")))
    assert res["ok"] is True and res["errors"] == [] and "reason" not in res


# ----------------------------------------------------------------------------
# MCP tool registry: every served tool comes from the TOOLS tuple of a module
# in k.TOOL_MODULES (kicad_server's own first), registered by tool_registry.
# Features add tools to a module's TOOLS only.
# ----------------------------------------------------------------------------
# Every tool the server registered before the module registry (bb87f63), in
# registration order: kicad_server's schematic + status tools, then
# pcb_ipc_tools, board_pipeline_tools, board_route_tools. A tool added to a
# FEATURE module is expected on top of this list; a tool added anywhere else
# must be added here.
SERVER_TOOL_COUNT = 25                   # kicad_server's own: create_schematic .. server_status
BASELINE_TOOLS = (
    "create_schematic", "load_schematic", "save_schematic", "close_schematic",
    "add_component", "add_components_batch", "add_wire", "add_label", "add_power_port",
    "wire_by_ref", "wire_pins", "label_pin", "power_pin", "no_connect_pin", "add_junction",
    "list_symbols", "list_nets", "schematic_netlist", "run_erc", "render_sch",
    "build_schematic_from_netlist", "build_pin_sidecars", "design_report",
    "test_kicad_connection", "server_status",
    # pcb_ipc_tools
    "create_pcb", "get_pcb_summary", "add_footprint_to_pcb", "add_footprints_batch",
    "move_footprint", "remove_footprint", "get_footprint_pads", "register_net", "add_trace",
    "add_via", "add_copper_zone", "validate_placement",
    # board_pipeline_tools
    "drc_pcb", "export_gerbers", "render_pcb", "build_board_from_netlist", "autoroute_board",
    "export_fab_package", "check_design_gates", "add_ground_stitching", "rescan_kicad_installs",
    # board_route_tools
    "open_board", "board_overview", "board_pads", "unrouted_connections", "route_net",
    "add_vias", "remove_routing", "route_check", "route_status", "save_board", "close_board",
    "list_open_boards",
)
FEATURE_MODULES = (library_tools, import_tools, review_tools, mechanical_tools)
_SPAWN_EVENTS = ("subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn", "os.exec",
                 "os.startfile")


def _names(server: FastMCP) -> list[str]:
    return [t.name for t in asyncio.run(server.list_tools())]


def _feature_tool_names() -> list[str]:
    return [fn.__name__ for mod in FEATURE_MODULES for fn in mod.TOOLS]


def probe_tool(text: str = "") -> dict:
    """Registry probe: echo the argument."""
    return {"ok": True, "text": text}


def _register(server: FastMCP) -> list[str]:
    return tool_registry.register_tool_modules(server, k.TOOL_MODULES)


def test_registered_tool_names_are_the_baseline_plus_feature_module_tools():
    added = _feature_tool_names()
    assert len(BASELINE_TOOLS) == 58 and len(set(BASELINE_TOOLS)) == 58
    assert not set(added) & set(BASELINE_TOOLS)          # FastMCP would drop a duplicate silently
    assert _names(k.mcp) == [*BASELINE_TOOLS, *added]


def test_tool_modules_is_the_one_explicit_list():
    assert k.TOOL_MODULES == (k, pcb_ipc_tools, board_pipeline_tools, board_route_tools,
                              *FEATURE_MODULES)
    assert [fn.__name__ for fn in k.TOOLS] == list(BASELINE_TOOLS[:SERVER_TOOL_COUNT])
    for mod in FEATURE_MODULES:
        assert isinstance(mod.TOOLS, tuple), mod.__name__
        assert "TOOLS" in (mod.__doc__ or "") and "ROADMAP_DECISIONS" in mod.__doc__, mod.__name__


def test_every_served_tool_comes_from_a_tools_tuple():
    # no second registration path (an @mcp.tool decorator) next to the registry
    assert _names(k.mcp) == [fn.__name__ for mod in k.TOOL_MODULES for fn in mod.TOOLS]


@pytest.mark.parametrize("module", FEATURE_MODULES, ids=lambda m: m.__name__)
def test_a_function_in_a_feature_module_tools_tuple_gets_registered(module, monkeypatch):
    monkeypatch.setattr(module, "TOOLS", (*module.TOOLS, probe_tool))
    server = FastMCP("registry-probe")
    names = _register(server)
    assert names.count("probe_tool") == 1
    assert _names(server) == names == [fn.__name__ for m in k.TOOL_MODULES for fn in m.TOOLS]
    result = asyncio.run(server.call_tool("probe_tool", {"text": "hi"}))
    blocks = result[0] if isinstance(result, tuple) else result   # (content, structured) or content
    assert json.loads(blocks[0].text) == {"ok": True, "text": "hi"}


def test_registration_refuses_a_name_two_modules_share(monkeypatch):
    def drc_pcb() -> dict:
        """A second drc_pcb."""
        return {}

    monkeypatch.setattr(review_tools, "TOOLS", (drc_pcb,))
    server = FastMCP("registry-probe")
    with pytest.raises(ValueError, match=r"review_tools\.TOOLS: .*'drc_pcb'.*taken by board_pipeline_tools"):
        _register(server)
    assert _names(server) == []                          # validated before registering anything


def test_registration_refuses_a_name_kicad_server_already_serves(monkeypatch):
    def design_report() -> dict:
        """Would be dropped by FastMCP in favour of kicad_server.design_report."""
        return {}

    monkeypatch.setattr(import_tools, "TOOLS", (design_report,))
    with pytest.raises(ValueError, match=r"import_tools\.TOOLS: .*'design_report'.*taken by kicad_server"):
        _register(FastMCP("registry-probe"))


def test_registration_refuses_the_same_function_listed_twice(monkeypatch):
    monkeypatch.setattr(library_tools, "TOOLS", (probe_tool, probe_tool))
    with pytest.raises(ValueError, match=r"library_tools\.TOOLS: .*'probe_tool'.*taken by library_tools"):
        _register(FastMCP("registry-probe"))


def test_a_kicad_server_helper_name_is_not_a_taken_tool_name(monkeypatch):
    # only TOOLS entries reserve a name: kicad_server's helpers (and the
    # registry function it imports) are not MCP tools, so a feature may use them
    def _kicad_python() -> dict:
        """Named like a kicad_server helper."""
        return {"ok": True}

    def register_tool_modules() -> dict:
        """Named like the registry function kicad_server imports."""
        return {"ok": True}

    assert callable(k._kicad_python) and callable(k.register_tool_modules)
    monkeypatch.setattr(review_tools, "TOOLS", (_kicad_python, register_tool_modules))
    names = _register(FastMCP("registry-probe"))
    assert names[-2:] == ["_kicad_python", "register_tool_modules"]


@pytest.mark.parametrize("bad, why", [(probe_tool, "must be a tuple"),
                                      ((lambda: {},), "not a named function"),
                                      (("probe_tool",), "not a named function"),
                                      ((FastMCP,), "not a named function")])
def test_registration_refuses_a_malformed_tools_tuple(bad, why, monkeypatch):
    monkeypatch.setattr(mechanical_tools, "TOOLS", bad)
    server = FastMCP("registry-probe")
    with pytest.raises(TypeError, match=rf"mechanical_tools\.TOOLS.*{why}"):
        _register(server)
    assert _names(server) == []


def test_messages_name_the_script_not_main(monkeypatch):
    # launched as `python kicad_server.py` the module is __main__; errors
    # still say kicad_server
    script = type(k)("__main__")
    script.__file__ = k.__file__
    script.TOOLS = (probe_tool, probe_tool)
    with pytest.raises(ValueError, match=r"^kicad_server\.TOOLS: .*taken by kicad_server"):
        tool_registry.register_tool_modules(FastMCP("registry-probe"), (script,))


_FRESH_IMPORT = r'''
import asyncio, json, sys
server_dir, seed = sys.argv[1], sys.argv[2]
sys.path[:0] = [server_dir, server_dir + "/tools"]
spawned = []
def audit(event, args):
    if event in SPAWN_EVENTS:
        spawned.append(event)
sys.addaudithook(audit)
sys.modules["pcbnew"] = None            # KiCad's SWIG module: any import of it fails
if seed:
    import library_tools
    def seeded_tool() -> dict:
        """Appended to library_tools.TOOLS before the server is imported."""
        return {"ok": True}
    library_tools.TOOLS = (*library_tools.TOOLS, seeded_tool)
import kicad_env, kicad_server
print(json.dumps({"tools": [t.name for t in asyncio.run(kicad_server.mcp.list_tools())],
                  "spawned": spawned, "discovered": kicad_env._INSTALLS is not None}))
'''


def _import_server_in_fresh_interpreter(tmp_path: Path, seed: bool) -> dict:
    """Import kicad_server in a new interpreter that sees no KiCad: no
    KICAD_PATH, empty Program Files / LOCALAPPDATA, no kicad-cli on PATH and
    pcbnew unimportable. Reports the served tool names, every process spawned
    during the import and whether KiCad discovery ran."""
    no_kicad = tmp_path / "no_kicad"
    no_kicad.mkdir()
    hidden = {"KICAD_PATH", "PROGRAMFILES", "LOCALAPPDATA", "PATH"}
    child_env = {key: val for key, val in os.environ.items() if key.upper() not in hidden}
    child_env.update(ProgramFiles=str(no_kicad), LOCALAPPDATA=str(no_kicad), PATH=os.pathsep.join(
        p for p in os.environ.get("PATH", "").split(os.pathsep)
        if p and not (Path(p) / env.CLI_NAME).is_file()))
    script = f"SPAWN_EVENTS = {_SPAWN_EVENTS!r}\n{_FRESH_IMPORT}"
    r = subprocess.run([sys.executable, "-c", script, str(Path(k.__file__).parent),
                        "seed" if seed else ""],
                       capture_output=True, text=True, timeout=120, env=child_env,
                       cwd=tmp_path, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, f"import failed:\n{r.stderr[-3000:]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_server_imports_cleanly_without_kicad(tmp_path):
    out = _import_server_in_fresh_interpreter(tmp_path, seed=False)
    assert out["spawned"] == [] and out["discovered"] is False   # KiCad is never touched
    assert out["tools"] == [*BASELINE_TOOLS, *_feature_tool_names()]


def test_a_tool_appended_to_a_feature_module_is_served_without_editing_the_server(tmp_path):
    out = _import_server_in_fresh_interpreter(tmp_path, seed=True)
    assert out["tools"].count("seeded_tool") == 1
    assert sorted(out["tools"]) == sorted([*BASELINE_TOOLS, *_feature_tool_names(), "seeded_tool"])


def test_the_launched_server_script_serves_every_tool_over_stdio(tmp_path):
    # the real launch path (scripts/launch-server.js): kicad_server.py runs as
    # __main__, so TOOL_MODULES must pick up the running script's own TOOLS
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, args=[k.__file__],
                                   env=dict(os.environ), cwd=str(tmp_path))

    async def served() -> list[str]:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return [t.name for t in (await session.list_tools()).tools]

    names = asyncio.run(asyncio.wait_for(served(), timeout=120))
    assert names == [*BASELINE_TOOLS, *_feature_tool_names()]
