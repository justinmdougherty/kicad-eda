"""library_tools: the F2 MCP tools for project-local libraries (ROADMAP D1):
author_footprint / author_symbol (presets lg290p / lc29h / opt-in
lg290p_lc29h, fp_spec_json templates, sym_author specs), validate_library,
resolve_libraries and compare_footprints.

Unit tier: KiCad is faked (the oldest kicad-cli, its `fp upgrade` / `sym
upgrade` parse checks and pcbnew FootprintLoad) and the stock libraries are
tmp dirs; kicad_env discovery raises if anything reaches it. Live tier
(test_live_*): the real oldest KiCad (9/10) parses and loads the LG290P
preset; skipped without one.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import decisions
import fp_author as fa
import kicad_env
import libtable as lt
import library_tools as lib
import part_presets as pp
import sym_author as sa
import toolchain as tc

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "server" / "library_tools.py"
NICK = "lg290p_breakout_parts"
LG_FP = "Quectel_LG290P"
LG_ID = f"{NICK}:{LG_FP}"
SOP_PARAMS = {"name": "SOIC8_custom", "body": {"w": 3.9, "h": 4.9}, "pins": 8, "pitch": 1.27,
              "span": 5.4, "pad_w": 0.6, "pad_l": 1.55}
SENSOR = {"name": "TMP_SENSOR", "reference": "U",
          "pins": [{"number": "1", "name": "VDD", "type": "power_in"},
                   {"number": "2", "name": "SDA", "type": "bidirectional"},
                   {"number": "3", "name": "GND", "type": "power_in"}]}


# --- fixtures: a fake KiCad, tmp stock libraries, a netlist -------------------------------
class FakeKicad:
    """Records the parse checks and loads the unit tier hands to 'KiCad'."""

    def __init__(self) -> None:
        self.parse_calls: list[tuple[str, str | None]] = []
        self.load_calls: list[tuple[str, str | None]] = []
        self.sym_calls: list[tuple[str, str | None]] = []
        self.parse_problems: list[str] = []
        self.load_error = ""
        self.sym_errors: list[str] = []

    def kicad_parse_problems(self, path, kicad_cli=None) -> list[str]:
        self.parse_calls.append((Path(path).name, kicad_cli))
        if kicad_cli is None:                    # as fp_author: falls back to the oldest, none
            return ["no kicad-cli found (install KiCad 9 or set KICAD_PATH): the KiCad parse "
                    "check did not run"]
        return list(self.parse_problems)

    def pcbnew_load(self, path, *, python=None) -> dict:
        self.load_calls.append((Path(path).name, python))
        if self.load_error:
            return {"ok": False, "error": self.load_error}
        pads = fa.read_footprint(path).pads
        return {"ok": True, "loaded": True, "pads": len(pads), "kicad_version": "9.0.4",
                "courtyard_outlines": 1, "numbers": [p.number for p in pads]}

    def kicad_resave(self, path, kicad_cli, timeout):
        self.sym_calls.append((Path(path).name, kicad_cli))
        if self.sym_errors:
            return {}, {}, list(self.sym_errors)
        pins, hidden = sa._pin_counts(Path(path).parent, Path(path).stem)
        return pins, hidden, []


@pytest.fixture
def stock(tmp_path: Path) -> Path:
    root = tmp_path / "stock"
    (root / "symbols").mkdir(parents=True)
    (root / "symbols" / "power.kicad_sym").write_text(
        '(kicad_symbol_lib (version 20241209) (generator "kicad_symbol_editor"))\n',
        encoding="utf-8")
    fa.write_footprint(fa.FootprintSpec(name="R_0603", pads=(
        fa.Pad("1", -0.8, 0, 0.8, 0.9), fa.Pad("2", 0.8, 0, 0.8, 0.9)),
        body=fa.Body(1.6, 0.8)), root / "footprints" / "Resistor_SMD.pretty")
    return root


@pytest.fixture(autouse=True)
def fake(request, monkeypatch, stock: Path) -> FakeKicad | None:
    if request.node.name.startswith("test_live_"):
        return None
    kicad = FakeKicad()

    def no_discovery(*_a, **_k):
        raise AssertionError("the unit tier must never discover KiCad")

    monkeypatch.setattr(kicad_env, "discover", no_discovery)
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": str(stock / "symbols"))
    monkeypatch.setattr(tc, "footprints_dir", lambda explicit="": str(stock / "footprints"))
    monkeypatch.setattr(tc, "kicad_python", lambda: "kicad9-python")
    monkeypatch.setattr(fa, "oldest_kicad_cli", lambda: ("kicad-cli-9", "kicad-cli 9.0.4"))
    monkeypatch.setattr(fa, "kicad_parse_problems", kicad.kicad_parse_problems)
    monkeypatch.setattr(fa, "pcbnew_load", kicad.pcbnew_load)
    monkeypatch.setattr(sa, "_kicad_resave", kicad.kicad_resave)
    return kicad


def write_netlist(folder: Path, design: dict | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "netlist.json"
    body = design if design is not None else {"name": "lg290p_breakout", "components": [],
                                              "nets": {}}
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def netlist(tmp_path: Path) -> Path:
    return write_netlist(tmp_path / "proj")


def block(netlist: Path) -> dict:
    return json.loads(netlist.read_text(encoding="utf-8")).get("libraries", {})


def table_rows(path: Path) -> dict[str, lt.LibRow]:
    return {row.name: row for row in lt.read_table(path).rows}


def assert_envelope(res: dict) -> None:
    assert isinstance(res["ok"], bool) and isinstance(res["assumptions"], list)
    assert decisions.validate_assumptions(res["assumptions"]) == []
    if not res["ok"]:
        assert isinstance(res["reason"], str) and res["reason"]


# --- the module and the registry -----------------------------------------------------------
def test_tools_are_the_five_library_tools_in_order():
    assert [fn.__name__ for fn in lib.TOOLS] == [
        "author_footprint", "author_symbol", "validate_library", "resolve_libraries",
        "compare_footprints"]
    assert all(fn.__doc__ and len(fn.__doc__) > 200 for fn in lib.TOOLS)
    assert "ROADMAP_DECISIONS" in lib.__doc__ and "TOOLS" in lib.__doc__


def test_the_module_spawns_no_process_itself():
    # every KiCad child goes through fp_author / sym_author (stdin DEVNULL, utf-8/replace)
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in n.names} | {n.module for n in ast.walk(tree)
                                     if isinstance(n, ast.ImportFrom)}
    assert not imported & {"subprocess", "os.system"}
    assert len(MODULE.read_text(encoding="utf-8").splitlines()) < 800


# --- author_footprint: presets -------------------------------------------------------------
def test_lg290p_preset_is_written_validated_registered_and_materialized(netlist, fake):
    res = lib.author_footprint(str(netlist), preset="lg290p")
    assert_envelope(res)
    assert res["ok"], res.get("reason")
    dest = netlist.parent / "libs" / f"{NICK}.pretty" / f"{LG_FP}.kicad_mod"
    assert res["lib_id"] == LG_ID and Path(res["path"]) == dest and res["state"] == "new"
    assert res["pads"] == 79 and res["written"] is True
    text = dest.read_text(encoding="utf-8")
    assert "(version 20241229)" in text and '(generator "kicad-eda")' in text
    assert block(netlist) == {"footprints": {NICK: f"libs/{NICK}.pretty"}}
    row = table_rows(netlist.parent / "fp-lib-table")[NICK]
    assert row.managed and row.uri == f"${{KIPRJMOD}}/libs/{NICK}.pretty"
    assert not (netlist.parent / "sym-lib-table").exists()
    # validated on the oldest kicad-cli and loaded by tc.kicad_python()'s pcbnew, staged
    assert fake.parse_calls == [(f"{LG_FP}.kicad_mod", "kicad-cli-9")]
    assert fake.load_calls == [(f"{LG_FP}.kicad_mod", "kicad9-python")]
    assert res["validation"]["problems"] == [] and res["validation"]["pcbnew"]["pads"] == 79


def test_lg290p_result_carries_the_preset_netlist_hints(netlist):
    res = lib.author_footprint(str(netlist), preset="lg290p")
    assert res["netlist_hints"] == pp.preset_hints("lg290p")
    assert res["netlist_hints"] == pp.preset_info("lg290p")["netlist_hints"]
    assert len(res["netlist_hints"]["preroutes"]) == 80            # 40 vias, two stubs each
    assert res["strategy"] == pp.PRESET_STRATEGY["lg290p"] and res["preset"] == "lg290p"
    assert len(res["pin_groups"]["GND"]) == 59
    topics = {a["id"]: a for a in res["assumptions"]}
    assert topics["author_footprint.library"]["value"].startswith(NICK)
    assert "apply_hints" in topics["author_footprint.netlist_hints"]["rationale"]


def test_lc29h_preset_has_no_escape_vias(netlist):
    res = lib.author_footprint(str(netlist), preset="lc29h")
    assert res["ok"] and res["lib_id"] == f"{NICK}:{pp.LC29H_NAME}" and res["pads"] == 24
    assert res["netlist_hints"] == pp.preset_hints("lc29h") and "preroutes" not in \
        res["netlist_hints"]


def test_the_combined_preset_is_opt_in_and_takes_a_stencil(netlist):
    refused = lib.author_footprint(str(netlist), preset="lg290p_lc29h")
    assert_envelope(refused)
    assert not refused["ok"] and "opt-in" in refused["reason"]
    res = lib.author_footprint(str(netlist), preset="lg290p_lc29h", opt_in=True,
                               params={"stencil": "lc29h"})
    assert res["ok"], res.get("reason")
    assert res["lib_id"] == f"{NICK}:{pp.COMBINED_NAMES['lc29h']}"
    assert res["netlist_hints"] == pp.preset_hints("lg290p_lc29h", opt_in=True)
    bad = lib.author_footprint(str(netlist), preset="lg290p", params={"stencil": "lc29h"})
    assert not bad["ok"] and "stencil" in bad["reason"]


def test_a_preset_can_be_renamed(netlist):
    res = lib.author_footprint(str(netlist), preset="lg290p", name="LG290P_Custom")
    assert res["ok"] and res["lib_id"] == f"{NICK}:LG290P_Custom"
    assert (Path(res["library"]) / "LG290P_Custom.kicad_mod").is_file()


# --- author_footprint: templates and refusals -------------------------------------------------
def test_a_template_footprint_from_json_params(netlist):
    res = lib.author_footprint(str(netlist), template="sop", params=json.dumps(SOP_PARAMS))
    assert res["ok"], res.get("reason")
    assert res["lib_id"] == f"{NICK}:SOIC8_custom" and res["pads"] == 8
    assert "netlist_hints" not in res
    named = lib.author_footprint(str(netlist), template="sop",
                                 params={k: v for k, v in SOP_PARAMS.items() if k != "name"},
                                 name="SOIC8_named")
    assert named["ok"] and named["lib_id"] == f"{NICK}:SOIC8_named"
    clash = lib.author_footprint(str(netlist), template="sop", params=SOP_PARAMS, name="Other")
    assert not clash["ok"] and "name" in clash["reason"]


@pytest.mark.parametrize("kwargs, why", [
    ({}, "exactly one of template"),
    ({"template": "sop", "preset": "lg290p"}, "exactly one of template"),
    ({"template": "sopp", "params": SOP_PARAMS}, "did you mean 'sop'"),
    ({"template": "sop", "params": {**SOP_PARAMS, "hieght": 1}}, "unknown key 'hieght'"),
    ({"template": "sop", "params": "{not json"}, "not valid JSON"),
    ({"template": "sop", "params": [1, 2]}, "params must be"),
    ({"preset": "lg29op"}, "did you mean 'lg290p'"),
])
def test_bad_footprint_requests_write_nothing(netlist, kwargs, why):
    before = netlist.read_text(encoding="utf-8")
    res = lib.author_footprint(str(netlist), **kwargs)
    assert_envelope(res)
    assert not res["ok"] and why in res["reason"], res["reason"]
    assert netlist.read_text(encoding="utf-8") == before
    assert not (netlist.parent / "libs").exists()


@pytest.mark.parametrize("bad", ["", "missing.json", "notjson", "array"])
def test_a_bad_netlist_is_refused(tmp_path, bad):
    (tmp_path / "notjson.json").write_text("{", encoding="utf-8")
    (tmp_path / "array.json").write_text("[]", encoding="utf-8")
    arg = {"": "", "missing.json": str(tmp_path / "missing.json"),
           "notjson": str(tmp_path / "notjson.json"), "array": str(tmp_path / "array.json")}[bad]
    for res in (lib.author_footprint(arg, preset="lg290p"),
                lib.author_symbol(arg, preset="lg290p"), lib.resolve_libraries(arg)):
        assert_envelope(res)
        assert not res["ok"] and "netlist" in res["reason"]


def test_an_existing_footprint_is_never_overwritten_unless_asked(netlist):
    first = lib.author_footprint(str(netlist), preset="lg290p")
    again = lib.author_footprint(str(netlist), preset="lg290p")
    assert again["ok"] and again["state"] == "unchanged" and again["written"] is False
    dest = Path(first["path"])
    edited = dest.read_text(encoding="utf-8").replace('(at -5.05 -7)', '(at -5.15 -7)', 1)
    assert edited != dest.read_text(encoding="utf-8")
    dest.write_text(edited, encoding="utf-8")
    refused = lib.author_footprint(str(netlist), preset="lg290p")
    assert not refused["ok"] and "overwrite=True" in refused["reason"]
    assert "1 of 79 matched pads differ" in refused["reason"]
    assert dest.read_text(encoding="utf-8") == edited
    forced = lib.author_footprint(str(netlist), preset="lg290p", overwrite=True)
    assert forced["ok"] and forced["state"] == "replaced"
    assert dest.read_text(encoding="utf-8") == fa.render(pp.PRESETS["lg290p"].footprint_fn())


@pytest.mark.parametrize("failure", ["parse", "load", "no_python", "no_cli"])
def test_a_footprint_that_fails_validation_is_not_written(netlist, fake, monkeypatch, failure):
    if failure == "parse":
        fake.parse_problems = ["kicad-cli 9.0.4 fp upgrade could not load X (rc=1)"]
    elif failure == "load":
        fake.load_error = "pcbnew.FootprintLoad returned None"
    elif failure == "no_python":
        monkeypatch.setattr(tc, "kicad_python", lambda: None)
        monkeypatch.setattr(tc, "kicad_python_error", lambda: "no bundled Python")
    else:
        monkeypatch.setattr(fa, "oldest_kicad_cli", lambda: (None, ""))
    res = lib.author_footprint(str(netlist), preset="lg290p")
    assert_envelope(res)
    assert not res["ok"] and "failed validation" in res["reason"]
    assert res["validation"]["problems"]
    assert not (netlist.parent / "libs").exists() and block(netlist) == {}
    assert not (netlist.parent / "fp-lib-table").exists()


# --- author_footprint: library selection ------------------------------------------------------
def test_library_by_nickname_path_and_declared_block_entry(tmp_path, netlist):
    by_nick = lib.author_footprint(str(netlist), preset="lc29h", library="SPS_parts")
    assert by_nick["ok"] and by_nick["lib_id"] == f"SPS_parts:{pp.LC29H_NAME}"
    assert block(netlist)["footprints"]["SPS_parts"] == "libs/SPS_parts.pretty"
    shared = lib.author_footprint(str(netlist), preset="lc29h", library="../shared/Shared.pretty")
    assert shared["ok"] and shared["lib_id"] == f"Shared:{pp.LC29H_NAME}"
    assert block(netlist)["footprints"]["Shared"] == "../shared/Shared.pretty"
    assert (tmp_path / "shared" / "Shared.pretty" / f"{pp.LC29H_NAME}.kicad_mod").is_file()
    assert {"SPS_parts", "Shared"} <= set(table_rows(netlist.parent / "fp-lib-table"))
    declared = write_netlist(tmp_path / "decl", {"name": "x", "libraries": {
        "footprints": {"Mine": "${KIPRJMOD}/parts/Mine.pretty"}}})
    res = lib.author_footprint(str(declared), preset="lc29h", library="Mine")
    assert res["ok"] and Path(res["library"]) == tmp_path / "decl" / "parts" / "Mine.pretty"
    assert block(declared)["footprints"] == {"Mine": "${KIPRJMOD}/parts/Mine.pretty"}


@pytest.mark.parametrize("library, why", [
    ("Resistor_SMD", "stock KiCad footprint library"),
    ("libs/X.kicad_sym", "ends in .pretty"),
    ("X.kicad_sym", "ends in .pretty"),
    ("bad:nick", "nickname"),
])
def test_bad_library_choices_are_refused(netlist, library, why):
    res = lib.author_footprint(str(netlist), preset="lc29h", library=library)
    assert not res["ok"] and why in res["reason"], res["reason"]
    assert block(netlist) == {}


def test_a_path_whose_nickname_is_declared_elsewhere_is_a_conflict(tmp_path):
    net = write_netlist(tmp_path / "p", {"name": "p", "libraries": {
        "footprints": {"Mine": "libs/Mine.pretty"}}})
    res = lib.author_footprint(str(net), preset="lc29h", library="other/Mine.pretty")
    assert not res["ok"] and "D1 conflict" in res["reason"]


def test_register_false_writes_the_file_only(netlist):
    res = lib.author_footprint(str(netlist), preset="lc29h", register=False)
    assert res["ok"] and res["registered"] is False and res["materialized"] is None
    assert Path(res["path"]).is_file() and block(netlist) == {}
    assert not (netlist.parent / "fp-lib-table").exists()
    assert any(a["id"] == "author_footprint.register" for a in res["assumptions"])


# --- author_symbol ----------------------------------------------------------------------------
def test_lg290p_symbol_after_its_footprint(netlist, fake):
    lib.author_footprint(str(netlist), preset="lg290p")
    res = lib.author_symbol(str(netlist), preset="lg290p")
    assert_envelope(res)
    assert res["ok"], res.get("reason")
    sym_lib = netlist.parent / "libs" / f"{NICK}.kicad_sym"
    assert res["lib_id"] == f"{NICK}:LG290P" and Path(res["library"]) == sym_lib
    assert res["footprint"] == LG_ID and f'"Footprint" "{LG_ID}"' in sym_lib.read_text("utf-8")
    assert res["pins"] == 79 and len(res["pin_groups"]["GND"]) == 59 and res["state"] == "new"
    sidecar = netlist.parent / "pins" / f"{NICK}_LG290P.pins.json"
    assert Path(res["pins_sidecar"]["sidecar"]) == sidecar and sidecar.is_file()
    assert block(netlist) == {"footprints": {NICK: f"libs/{NICK}.pretty"},
                              "symbols": {NICK: f"libs/{NICK}.kicad_sym"}}
    assert table_rows(netlist.parent / "sym-lib-table")[NICK].managed
    assert NICK in table_rows(netlist.parent / "fp-lib-table")        # both stay materialized
    assert fake.sym_calls == [(f"{NICK}.kicad_sym", "kicad-cli-9")]
    assert res["validation"]["symbols"]["LG290P"]["pins"] == 79
    footprint_default = {a["id"]: a for a in res["assumptions"]}["author_symbol.footprint"]
    assert footprint_default["confidence"] == "high" and footprint_default["value"] == LG_ID


def test_a_preset_symbol_before_its_footprint_still_names_the_default(netlist):
    res = lib.author_symbol(str(netlist), preset="lc29h")
    assert res["ok"] and res["footprint"] == f"{NICK}:{pp.LC29H_NAME}"
    note = {a["id"]: a for a in res["assumptions"]}["author_symbol.footprint"]
    assert note["confidence"] == "med" and "author_footprint" in note["rationale"]


def test_the_footprint_default_follows_the_library_that_holds_it(netlist):
    lib.author_footprint(str(netlist), preset="lc29h", library="SPS_parts")
    res = lib.author_symbol(str(netlist), preset="lc29h")
    assert res["ok"] and res["footprint"] == f"SPS_parts:{pp.LC29H_NAME}"
    explicit = lib.author_symbol(str(netlist), preset="lc29h", footprint="Other:FP",
                                 overwrite=True)
    assert explicit["ok"] and explicit["footprint"] == "Other:FP"


def test_a_symbol_from_a_spec(netlist):
    res = lib.author_symbol(str(netlist), spec=json.dumps(SENSOR),
                            footprint="Package_TO_SOT_SMD:SOT-23", library="SPS_parts")
    assert res["ok"], res.get("reason")
    assert res["lib_id"] == "SPS_parts:TMP_SENSOR" and res["footprint"] == \
        "Package_TO_SOT_SMD:SOT-23"
    bare = lib.author_symbol(str(netlist), spec={**SENSOR, "name": "BARE"})
    assert bare["ok"] and bare["footprint"] == ""
    note = {a["id"]: a for a in bare["assumptions"]}["author_symbol.footprint"]
    assert note["confidence"] == "med" and note["override"] == "components[*].footprint"


@pytest.mark.parametrize("kwargs, why", [
    ({}, "exactly one of spec"),
    ({"spec": SENSOR, "preset": "lg290p"}, "exactly one of spec"),
    ({"spec": {**SENSOR, "pins": [{"number": "1"}]}}, "'name' is required"),
    ({"spec": {**SENSOR, "colour": "red"}}, "unknown symbol key"),
    ({"spec": {**SENSOR, "pins": [{"number": "1", "name": "A", "type": "wobbly"}]}},
     "electrical type"),
    ({"preset": "lg290p_lc29h"}, "opt-in"),
    ({"spec": SENSOR, "footprint": "no-colon"}, "nick:name"),
    ({"spec": SENSOR, "library": "libs/X.pretty"}, "ends in .kicad_sym"),
])
def test_bad_symbol_requests_write_nothing(netlist, kwargs, why):
    res = lib.author_symbol(str(netlist), **kwargs)
    assert_envelope(res)
    assert not res["ok"] and why in res["reason"], res["reason"]
    assert not (netlist.parent / "libs").exists() and block(netlist) == {}


def test_symbols_are_added_kept_and_never_replaced_unless_asked(netlist):
    assert lib.author_symbol(str(netlist), spec=SENSOR)["state"] == "new"
    assert lib.author_symbol(str(netlist), spec=SENSOR)["state"] == "unchanged"
    other = lib.author_symbol(str(netlist), spec={**SENSOR, "name": "OTHER"})
    assert other["ok"] and other["state"] == "added"
    changed = {**SENSOR, "pins": [*SENSOR["pins"], {"number": "4", "name": "ALERT",
                                                    "type": "output"}]}
    refused = lib.author_symbol(str(netlist), spec=changed)
    assert not refused["ok"] and "overwrite=True" in refused["reason"]
    forced = lib.author_symbol(str(netlist), spec=changed, overwrite=True)
    assert forced["ok"] and forced["state"] == "replaced" and forced["pins"] == 4
    names = sa._pin_counts(Path(forced["library"]).parent, Path(forced["library"]).stem)[0]
    assert names == {"TMP_SENSOR": 4, "OTHER": 3}


def test_pins_sidecar_and_registration_can_be_skipped(netlist):
    res = lib.author_symbol(str(netlist), spec=SENSOR, pins_sidecar=False, register=False)
    assert res["ok"] and res["pins_sidecar"] is None and res["registered"] is False
    assert not (netlist.parent / "pins").exists() and block(netlist) == {}


def test_a_symbol_that_kicad_rejects_is_not_written(netlist, fake):
    fake.sym_errors = ["kicad-cli sym upgrade rejected x.kicad_sym (rc=1)"]
    res = lib.author_symbol(str(netlist), preset="lg290p")
    assert not res["ok"] and "failed validation" in res["reason"]
    assert res["validation"]["problems"] == fake.sym_errors
    assert not (netlist.parent / "libs").exists() and not (netlist.parent / "pins").exists()


def test_a_symbol_library_in_another_dialect_is_refused(netlist):
    path = netlist.parent / "libs" / f"{NICK}.kicad_sym"
    path.parent.mkdir(parents=True)
    path.write_text('(kicad_symbol_lib (version 20260101) (generator "x"))\n', encoding="utf-8")
    res = lib.author_symbol(str(netlist), spec=SENSOR)
    assert not res["ok"] and "only the KiCad 9 dialect" in res["reason"]


# --- validate_library -------------------------------------------------------------------------
def test_validate_a_footprint_library_and_one_file(netlist, fake):
    made = lib.author_footprint(str(netlist), preset="lg290p")
    lib.author_footprint(str(netlist), preset="lc29h")
    res = lib.validate_library(made["library"])
    assert_envelope(res)
    assert res["ok"] and res["kind"] == "footprint" and res["problems"] == []
    assert res["pads"] == {LG_FP: 79, pp.LC29H_NAME: 24}
    assert res["footprints"][LG_FP]["pcbnew"]["pads"] == 79
    one = lib.validate_library(made["path"], expected_pads=79)
    assert one["ok"] and one["pads"] == {LG_FP: 79}
    wrong = lib.validate_library(made["path"], expected_pads=80)
    assert not wrong["ok"] and "79 distinct pad numbers, expected 80" in wrong["reason"]
    named = lib.validate_library(made["library"], expected_pads={LG_FP: 79, "Nope": 3})
    assert not named["ok"] and "Nope" in named["reason"]


def test_validate_a_symbol_library(netlist):
    made = lib.author_symbol(str(netlist), preset="lg290p")
    res = lib.validate_library(made["library"], expected_pads=79)
    assert res["ok"] and res["kind"] == "symbol" and res["pins"] == {"LG290P": 79}
    assert res["symbols"]["LG290P"]["hidden_pins"] == 58
    wrong = lib.validate_library(made["library"], expected_pads={"LG290P": 78})
    assert not wrong["ok"] and "LG290P" in wrong["reason"]


@pytest.mark.parametrize("what, why", [
    ("missing", "not found"), ("empty", "no .kicad_mod"), ("other", "not a footprint"),
    ("bool", "expected_pads"), ("negative", "expected_pads")])
def test_validate_library_refusals(tmp_path, what, why):
    (tmp_path / "Empty.pretty").mkdir()
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    path = {"missing": tmp_path / "Nope.pretty", "empty": tmp_path / "Empty.pretty",
            "other": tmp_path / "notes.txt"}.get(what, tmp_path / "Empty.pretty")
    expected = {"bool": True, "negative": -1}.get(what)
    res = lib.validate_library(str(path), expected_pads=expected)
    assert_envelope(res)
    assert not res["ok"] and why in res["reason"], res["reason"]


# --- resolve_libraries ------------------------------------------------------------------------
def test_resolve_reports_every_nickname_with_source_path_and_errors(tmp_path, stock):
    net = write_netlist(tmp_path / "r", {"name": "r", "components": [
        {"ref": "R1", "lib_id": "Device:R", "footprint": "Resistor_SMD:R_0603"},
        {"ref": "U1", "lib_id": "Gone:X", "footprint": "r_parts:Y"}]})
    lib.author_footprint(str(net), preset="lc29h")
    res = lib.resolve_libraries(str(net))
    assert_envelope(res)
    fps, syms = res["footprints"], res["symbols"]
    assert fps["r_parts"]["source"] == "netlist" and fps["r_parts"]["exists"]
    assert fps["r_parts"]["generated"] is True and fps["r_parts"]["used"] is True
    assert fps["Resistor_SMD"]["source"] == "stock" and fps["Resistor_SMD"]["errors"] == []
    assert Path(fps["Resistor_SMD"]["path"]) == stock / "footprints" / "Resistor_SMD.pretty"
    assert syms["Gone"]["source"] is None and syms["Gone"]["errors"]
    assert syms["Device"]["source"] is None                     # the tmp stock has no Device
    assert syms["power"]["source"] == "stock"
    assert not res["ok"] and "Gone" in res["reason"] and len(res["errors"]) == 2
    assert res["stock"]["footprints"] == str(stock / "footprints")


def test_resolve_can_materialize_and_rejects_a_malformed_block(tmp_path):
    net = write_netlist(tmp_path / "m", {"name": "m", "libraries": {
        "footprints": {"m_parts": "libs/m_parts.pretty"}}})
    (tmp_path / "m" / "libs" / "m_parts.pretty").mkdir(parents=True)
    res = lib.resolve_libraries(str(net), materialize=True)
    assert res["ok"] and res["materialized"]["written"] == [str(tmp_path / "m" / "fp-lib-table")]
    assert table_rows(tmp_path / "m" / "fp-lib-table")["m_parts"].managed
    again = lib.resolve_libraries(str(net), materialize=True)
    assert again["ok"] and again["materialized"]["written"] == []
    bad = write_netlist(tmp_path / "b", {"name": "b", "libraries": {"footprints": ["x"]}})
    refused = lib.resolve_libraries(str(bad))
    assert not refused["ok"] and "libraries.footprints" in refused["reason"]


def test_a_project_table_conflict_is_reported_not_materialized(tmp_path):
    net = write_netlist(tmp_path / "c", {"name": "c", "libraries": {
        "footprints": {"X": "libs/X.pretty"}}})
    (tmp_path / "c" / "libs" / "X.pretty").mkdir(parents=True)
    lt.write_table(tmp_path / "c" / "fp-lib-table", lt.LibTable("fp_lib_table", (
        lt.LibRow("X", "${KIPRJMOD}/elsewhere/X.pretty"),)))
    res = lib.resolve_libraries(str(net), materialize=True)
    assert not res["ok"] and "same nickname, different URI" in res["reason"]
    assert "error" in res["materialized"] and res["footprints"]["X"]["errors"]


# --- compare_footprints -----------------------------------------------------------------------
def test_compare_lib_ids_and_paths(netlist, stock):
    made = lib.author_footprint(str(netlist), preset="lg290p")
    same = lib.compare_footprints(LG_ID, made["path"], netlist=str(netlist))
    assert_envelope(same)
    assert same["ok"] and same["identical"] and same["summary"] == "identical"
    assert same["sources"]["a"] == {"kind": "lib_id", "lib_id": LG_ID, "source": "netlist",
                                    "path": made["path"]}
    rel = lib.compare_footprints(f"libs/{NICK}.pretty/{LG_FP}.kicad_mod", LG_ID,
                                 netlist=str(netlist))
    assert rel["ok"] and rel["identical"]
    lc = lib.author_footprint(str(netlist), preset="lc29h")
    diff = lib.compare_footprints(made["path"], lc["path"])
    assert diff["ok"] and not diff["identical"] and diff["pads"] == [79, 24]
    assert len(diff["unmatched_a"]) >= 55                         # the inner GND grid at least
    stock_only = lib.compare_footprints("Resistor_SMD:R_0603", "Resistor_SMD:R_0603")
    assert stock_only["ok"] and stock_only["identical"]
    assert stock_only["sources"]["a"]["source"] == "stock"
    assert any(a["id"] == "compare_footprints.stock_only" for a in stock_only["assumptions"])


@pytest.mark.parametrize("a, why", [
    ("", "lib_id"), ("NoColon", "neither"), ("Nope:X", "'Nope'"), ("Resistor_SMD:Nope", "Nope"),
    ("missing.kicad_mod", "not found")])
def test_compare_refusals(netlist, a, why):
    res = lib.compare_footprints(a, "Resistor_SMD:R_0603", netlist=str(netlist))
    assert_envelope(res)
    assert not res["ok"] and why in res["reason"], res["reason"]
    negative = lib.compare_footprints("Resistor_SMD:R_0603", "Resistor_SMD:R_0603",
                                      tolerance=-1)
    assert not negative["ok"] and "tolerance" in negative["reason"]


# --- live: the oldest KiCad (9/10) parses and loads what the tools write ----------------------
def _stable_install() -> kicad_env.KiCadInstall | None:
    found = kicad_env.discover()
    oldest = min(found, key=lambda i: i.version_tuple) if found else None
    return oldest if oldest and oldest.version_tuple[0] <= 10 else None


@pytest.fixture(scope="module")
def live_kicad() -> kicad_env.KiCadInstall:
    inst = _stable_install()
    if inst is None or tc.kicad_python() is None:
        pytest.skip("no KiCad 9/10 install with pcbnew (the D0 dialect floor)")
    return inst


def test_live_lg290p_preset_round_trip(live_kicad, tmp_path):
    net = write_netlist(tmp_path / "lg290p_breakout")
    fp = lib.author_footprint(str(net), preset="lg290p")
    assert fp["ok"], fp.get("reason")
    assert fp["validation"]["kicad_cli"] == str(live_kicad.kicad_cli)
    assert fp["validation"]["pcbnew"]["pads"] == 79
    sym = lib.author_symbol(str(net), preset="lg290p")
    assert sym["ok"], sym.get("reason")
    assert sym["footprint"] == LG_ID
    assert block(net) == {"footprints": {NICK: f"libs/{NICK}.pretty"},
                          "symbols": {NICK: f"libs/{NICK}.kicad_sym"}}
    for table in ("fp-lib-table", "sym-lib-table"):
        assert table_rows(net.parent / table)[NICK].managed
    footprints = lib.validate_library(fp["library"])
    assert footprints["ok"] and footprints["problems"] == [], footprints.get("reason")
    assert footprints["pads"] == {LG_FP: 79}
    symbols = lib.validate_library(sym["library"], expected_pads=79)
    assert symbols["ok"] and symbols["problems"] == [] and symbols["pins"] == {"LG290P": 79}
    resolved = lib.resolve_libraries(str(net))
    assert resolved["footprints"][NICK]["source"] == "netlist"
    assert resolved["symbols"][NICK]["generated"] is True
    same = lib.compare_footprints(LG_ID, fp["path"], netlist=str(net))
    assert same["ok"] and same["identical"]
