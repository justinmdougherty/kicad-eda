"""F46-17: the schematic builders and the design review honour the D4
component flags (``board_only`` / ``dnp`` / ``in_bom``), detect ground nets
beyond the literal "GND" and fall back on a component's own ``pins`` map.

Tiers: the synthetic-library tests (tests/fixtures/symbols and libraries
authored with sym_author) never skip; the stock-library spot checks skip
without a KiCad install; the live ERC needs kicad-cli 9; the API-backend build
needs the KiCad 11 nightly (SchematicApi.available()).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import design_report
import kicad_env as env
import sch_build
import toolchain as tc
from backends import kipy_bridge as kb
from backends import sch_netlist_build as snb
from backends.errors import SchematicApiError
from conftest import load_example
from fixtures.kicad_install import KICAD, requires_symbols

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
SCH_BUILD = ROOT / "server" / "tools" / "sch_build.py"
CENTRAL_PINS = ROOT / "server" / "pins"
FIXTURE_SYMBOLS = TESTS / "fixtures" / "symbols"
PYTHON = Path(sys.executable)          # the interpreter running the tests (venv or image)
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CLI_ERROR_RE = re.compile(r"(^|\s)Error:", re.M)
# sch_build before the D4 flags (roadmap wave 1): the examples must still build
# byte-identical schematics (UUIDs aside)
BASELINE_COMMIT = "6a8f94d"
BOARD_ONLY = ["H1", "TP1"]


# --- the fixture design ----------------------------------------------------------------

def _flags_design() -> dict:
    """A buffered op-amp (TestLib) with a dnp resistor, an in_bom=false
    resistor, a board-only NPTH hole (no lib_id, no net) and a board-only test
    point on GND whose lib_id is in no library (it must never be loaded)."""
    return {"name": "flags", "components": [
        {"ref": "U1", "lib_id": "TestLib:DerivedAmp", "value": "amp", "footprint": "F:SO8"},
        {"ref": "R1", "lib_id": "TestLib:PosFileR", "value": "0R", "footprint": "F:R",
         "dnp": True},
        {"ref": "H1", "footprint": "MountingHole:MountingHole_3.2mm_M3", "board_only": True,
         "at": [3.5, 3.5, 0]},
        {"ref": "R2", "lib_id": "TestLib:PosFileR", "value": "10k", "footprint": "F:R",
         "in_bom": False},
        {"ref": "TP1", "lib_id": "Connector:TestPoint", "value": "GND",
         "footprint": "TestPoint:TestPoint_Pad_D1.0mm", "board_only": True}],
        "nets": {"VCC": [["U1", "8"], ["R1", "1"], ["R2", "1"]],
                 "GND": [["U1", "4"], ["R1", "2"], ["TP1", "1"], ["R2", "2"]],
                 "OUT": [["U1", "1"], ["U1", "2"]]},
        "no_connect": [["U1", "3"], ["U1", "5"], ["U1", "6"], ["U1", "7"]]}


def _write(path: Path, design: dict) -> Path:
    path.write_text(json.dumps(design), encoding="utf-8")
    return path


def _sexp(text: str) -> list:
    import sexpdata
    return sexpdata.loads(text)


def _placements(text: str) -> dict[str, list[dict]]:
    """{ref: [{unit, in_bom, on_board, dnp, lib_id}]} of every placed symbol."""
    from symlib import first, is_, kids
    out: dict[str, list[dict]] = {}
    for node in (n for n in _sexp(text) if is_(n, "symbol")):
        ref = next(p[2] for p in kids(node, "property") if p[1] == "Reference")
        flags = {key: first(node, key)[1].value() for key in ("in_bom", "on_board", "dnp")}
        out.setdefault(ref, []).append(
            {"unit": first(node, "unit")[1], "lib_id": first(node, "lib_id")[1], **flags})
    return out


def _labels(text: str) -> list[str]:
    from symlib import is_
    return [n[1] for n in _sexp(text) if is_(n, "label")]


def _embedded(text: str) -> list[str]:
    from symlib import first, kids
    lib_symbols = first(_sexp(text), "lib_symbols")
    return sorted(s[1] for s in kids(lib_symbols, "symbol"))


@pytest.fixture
def flags_build(tmp_path: Path) -> tuple[dict, str]:
    out = tmp_path / "flags.kicad_sch"
    result = sch_build.build(str(_write(tmp_path / "flags.json", _flags_design())), str(out),
                             str(FIXTURE_SYMBOLS))
    return result, out.read_text(encoding="utf-8") if out.is_file() else ""


# --- file backend (sch_build) ----------------------------------------------------------

def test_board_only_parts_get_no_symbol_and_their_pads_no_label(flags_build):
    result, text = flags_build
    assert result["ok"] is True, result
    assert (result["components"], result["nets"], result["board_only"]) == (3, 3, BOARD_ONLY)
    placed = _placements(text)
    assert sorted(placed) == ["#FLG01", "#FLG02", "R1", "R2", "U1"]
    assert "Connector:TestPoint" not in _embedded(text)
    assert not any(token in text for token in ("TP1", "H1", "MountingHole", "TestPoint"))
    labels = _labels(text)
    # GND: U1.4, R1.2, R2.2 plus its PWR_FLAG label -- never one for TP1.1
    assert labels.count("GND") == 3 + 1 and labels.count("VCC") == 3 + 1


def test_dnp_and_in_bom_are_written_on_every_placement(flags_build):
    _result, text = flags_build
    placed = _placements(text)
    assert placed["R1"] == [{"unit": 1, "lib_id": "TestLib:PosFileR", "in_bom": "yes",
                             "on_board": "yes", "dnp": "yes"}]
    assert placed["R2"] == [{"unit": 1, "lib_id": "TestLib:PosFileR", "in_bom": "no",
                             "on_board": "yes", "dnp": "no"}]
    assert [(p["unit"], p["in_bom"], p["dnp"]) for p in placed["U1"]] == [
        (1, "yes", "no"), (2, "yes", "no"), (3, "yes", "no")]
    assert all((p["in_bom"], p["dnp"]) == ("yes", "no") for p in placed["#FLG01"])


def test_the_cli_builds_a_board_only_part_without_lib_id(tmp_path: Path):
    """The T04 finding: a board_only part without lib_id raised KeyError."""
    out = tmp_path / "cli.kicad_sch"
    proc = subprocess.run([str(PYTHON), str(SCH_BUILD), str(_write(tmp_path / "cli.json",
                                                                    _flags_design())),
                           str(out), str(FIXTURE_SYMBOLS), ""],
                          capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == {
        "ok": True, "sch": str(out), "components": 3, "nets": 3, "warnings": [],
        "board_only": BOARD_ONLY}


def test_board_only_pads_get_no_no_connect_and_a_net_of_them_vanishes(tmp_path: Path):
    design = _flags_design()
    design["components"].append({"ref": "H2", "footprint": "MountingHole:MountingHole_3.2mm_M3_Pad",
                                 "board_only": True})
    design["nets"]["CHASSIS"] = [["H2", "1"]]                 # only board-only pads
    design["no_connect"].append(["TP1", "2"])
    design["power_flags"] = ["CHASSIS"]                       # declared, but not in the schematic
    out = tmp_path / "nc.kicad_sch"
    result = sch_build.build(str(_write(tmp_path / "nc.json", design)), str(out),
                             str(FIXTURE_SYMBOLS))
    assert result["ok"] is True and result["nets"] == 3, result
    text = out.read_text(encoding="utf-8")
    assert text.count("\t(no_connect ") == 4
    assert "CHASSIS" not in text and "#FLG03" not in text


def test_schematic_design_is_a_new_dict_and_leaves_the_netlist_alone():
    design = _flags_design()
    before = json.dumps(design)
    view = sch_build.schematic_design(design)
    assert json.dumps(design) == before
    assert [c["ref"] for c in view["components"]] == ["U1", "R1", "R2"]
    assert view["nets"]["GND"] == [["U1", "4"], ["R1", "2"], ["R2", "2"]]
    plain = {"components": [{"ref": "R1", "lib_id": "Device:R"}], "nets": {"EMPTY": []}}
    assert sch_build.schematic_design(plain)["nets"] == {"EMPTY": []}   # kept as before
    assert sch_build.board_only_refs(design) == BOARD_ONLY


@pytest.mark.parametrize("key,value", [("dnp", "yes"), ("in_bom", 0), ("board_only", "true")])
def test_a_non_boolean_flag_is_refused_and_nothing_is_written(tmp_path: Path, key, value):
    design = _flags_design()
    design["components"][1][key] = value
    out = tmp_path / "bad.kicad_sch"
    proc = subprocess.run([str(PYTHON), str(SCH_BUILD), str(_write(tmp_path / "bad.json", design)),
                           str(out), str(FIXTURE_SYMBOLS), ""],
                          capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    assert proc.returncode == 1
    res = json.loads(proc.stdout.strip().splitlines()[-1])
    assert res["ok"] is False
    assert res["error"] == f"ValueError: R1: '{key}' must be true or false, got {value!r}"
    assert not out.exists()


def _normalised(text: str) -> str:
    return UUID_RE.sub("UUID", text)


def _with_default_flags(design: dict) -> dict:
    return {**design, "components": [{**c, "board_only": False, "dnp": False, "in_bom": True}
                                     for c in design["components"]]}


def _build_text(netlist: Path, out: Path, symbols: Path, script: Path = SCH_BUILD) -> str:
    proc = subprocess.run([str(PYTHON), str(script), str(netlist), str(out), str(symbols),
                           str(CENTRAL_PINS)],
                          capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return _normalised(out.read_text(encoding="utf-8"))


def test_explicit_default_flags_change_nothing_synthetic(tmp_path: Path):
    flagged = _flags_design()
    design = {**flagged,
              "components": [{k: v for k, v in c.items() if k not in ("dnp", "in_bom")}
                             for c in flagged["components"] if not c.get("board_only")],
              "nets": {**flagged["nets"],
                       "GND": [p for p in flagged["nets"]["GND"] if p[0] != "TP1"]}}
    plain = _build_text(_write(tmp_path / "a.json", design), tmp_path / "a.kicad_sch",
                        FIXTURE_SYMBOLS)
    explicit = _build_text(_write(tmp_path / "b.json", _with_default_flags(design)),
                           tmp_path / "b.kicad_sch", FIXTURE_SYMBOLS)
    assert plain == explicit
    # U1's 3 units, R1, R2 and the 2 PWR_FLAGs keep the pre-D4 attribute lines
    assert plain.count("\t\t(in_bom yes)\n\t\t(on_board yes)\n\t\t(dnp no)\n") == 7


SPOT_CHECKS = ["lm358_buffer", "stm32f103_dev"]


@requires_symbols
@pytest.mark.parametrize("name", SPOT_CHECKS)
def test_examples_with_explicit_default_flags_build_the_same_schematic(tmp_path: Path, name):
    netlist = ROOT / "examples" / name / "netlist.json"
    plain = _build_text(netlist, tmp_path / "plain.kicad_sch", KICAD.symbols)
    explicit = _build_text(_write(tmp_path / "explicit.json",
                                  _with_default_flags(load_example(name))),
                           tmp_path / "explicit.kicad_sch", KICAD.symbols)
    assert plain == explicit
    assert "(dnp yes)" not in plain and "(in_bom no)" not in plain


def _baseline_tools(dest: Path) -> Path:
    """sch_build.py + symlib.py as of BASELINE_COMMIT, or skip."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not installed")
    for name in ("sch_build.py", "symlib.py"):
        proc = subprocess.run([git, "show", f"{BASELINE_COMMIT}:./server/tools/{name}"],
                              cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            pytest.skip(f"baseline {BASELINE_COMMIT} not available: {proc.stderr.strip()[:200]}")
        (dest / name).write_text(proc.stdout, encoding="utf-8")
    return dest / "sch_build.py"


@requires_symbols
@pytest.mark.parametrize("name", SPOT_CHECKS)
def test_examples_build_byte_identical_to_the_baseline_except_uuids(tmp_path: Path, name):
    base_dir = tmp_path / "baseline"
    base_dir.mkdir()
    script = _baseline_tools(base_dir)
    netlist = ROOT / "examples" / name / "netlist.json"
    now = _build_text(netlist, tmp_path / "now.kicad_sch", KICAD.symbols)
    then = _build_text(netlist, tmp_path / "then.kicad_sch", KICAD.symbols, script)
    assert now == then


# --- live: kicad-cli 9 ERC of the fixture schematic ------------------------------------

def _kicad9_cli() -> Path:
    found = [i for i in env.discover() if i.version_tuple[0] == 9]
    if not found:
        pytest.skip("kicad-cli 9 not installed")
    return found[0].kicad_cli


def _kicad_cli(cli: Path, args: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run([str(cli), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=180, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0 and not CLI_ERROR_RE.search(proc.stdout + proc.stderr), \
        proc.stdout + proc.stderr
    return proc


def test_fixture_schematic_is_erc_clean_on_kicad_9(flags_build, tmp_path: Path):
    cli = _kicad9_cli()
    _result, text = flags_build
    sch = tmp_path / "erc" / "flags.kicad_sch"
    sch.parent.mkdir()
    sch.write_text(text, encoding="utf-8")
    report = sch.with_suffix(".erc.json")
    _kicad_cli(cli, ["sch", "erc", "--format", "json", "--severity-error", "-o", str(report),
                     str(sch)])
    erc = json.loads(report.read_text(encoding="utf-8"))
    errors = [v for s in erc["sheets"] for v in s["violations"] if v["severity"] == "error"]
    assert errors == [], json.dumps(errors, indent=1)


def test_kicad_9_reads_the_flags_back(flags_build, tmp_path: Path):
    """KiCad's own netlist export: R1 is DNP, R2 is out of the BOM, the
    board-only parts are absent."""
    import xml.etree.ElementTree as ET
    cli = _kicad9_cli()
    _result, text = flags_build
    sch = tmp_path / "flags.kicad_sch"
    sch.write_text(text, encoding="utf-8")
    xml_out = tmp_path / "flags.xml"
    _kicad_cli(cli, ["sch", "export", "netlist", "--format", "kicadxml", "-o", str(xml_out),
                     str(sch)])
    comps = {c.get("ref"): {p.get("name") for p in c.iter("property")}
             for c in ET.parse(xml_out).getroot().iter("comp")}
    assert sorted(comps) == ["R1", "R2", "U1"]
    assert "dnp" in comps["R1"] and "exclude_from_bom" not in comps["R1"]
    assert "exclude_from_bom" in comps["R2"] and "dnp" not in comps["R2"]
    assert not {"dnp", "exclude_from_bom"} & comps["U1"]


# --- API backend (backends.sch_netlist_build) ------------------------------------------

def test_validate_design_accepts_a_board_only_part_without_lib_id():
    snb.validate_design(_flags_design())
    with pytest.raises(SchematicApiError, match=r"components\[0\] \(U9\) needs a 'lib_id'"):
        snb.validate_design({"components": [{"ref": "U9", "footprint": "F"}]})


def test_validate_design_refuses_a_non_boolean_flag():
    design = _flags_design()
    design["components"][1]["dnp"] = "yes"
    with pytest.raises(SchematicApiError,
                       match=r"components\[1\]: R1: 'dnp' must be true or false, got 'yes'"):
        snb.validate_design(design)


def test_assembly_attributes_only_turn_attributes_on():
    assert snb.assembly_attributes({"ref": "R1", "dnp": True}) == {"do_not_populate": True}
    assert snb.assembly_attributes({"ref": "R2", "in_bom": False}) == {
        "exclude_from_bill_of_materials": True}
    assert snb.assembly_attributes({"ref": "R3", "dnp": True, "in_bom": False}) == {
        "do_not_populate": True, "exclude_from_bill_of_materials": True}
    assert snb.assembly_attributes({"ref": "R4", "dnp": False, "in_bom": True}) == {}


class _FakeSchematic:
    """GetSymbols / UpdateItems over real kipy instances; ``apply`` False makes
    KiCad answer with the attributes cleared (a server that ignores them)."""

    def __init__(self, refs: list[tuple[str, int]], apply: bool = True) -> None:
        self.instances = []
        for ref, unit in refs:
            inst = kb.st.SchematicSymbolInstance()
            inst.reference = ref
            inst.unit = unit
            inst.definition.id.name = f"{ref}_{unit}"          # a privately renamed copy
            self.instances.append(inst)
        self.apply = apply
        self.updates: list[list] = []

    def get_symbols(self) -> list:
        return list(self.instances)

    def update_items(self, items: list) -> list:
        self.updates.append(list(items))
        if self.apply:
            return list(items)
        return [self._ignored(i) for i in items]

    @staticmethod
    def _ignored(inst):
        echo = kb.st.SchematicSymbolInstance(proto=inst.proto)
        echo.attributes = kb.st.SchematicSymbolAttributes()
        return echo


def _fake_api(sch: _FakeSchematic) -> SimpleNamespace:
    reads: list[int] = []
    return SimpleNamespace(path="x.kicad_sch", reads=reads,
                           session=SimpleNamespace(schematic_for=lambda _p: sch),
                           symbols=lambda: reads.append(1) or [])


requires_kipy = pytest.mark.skipif(bool(kb.IMPORT_ERROR), reason=kb.IMPORT_ERROR or "")


@requires_kipy
def test_apply_assembly_flags_sets_every_unit_through_kipy():
    from backends import sch_items as items
    sch = _FakeSchematic([("U1", 1), ("U1", 2), ("R1", 1), ("R2", 1)])
    api = _fake_api(sch)
    comps = [{"ref": "U1", "lib_id": "Amplifier_Operational:LM358", "dnp": True},
             {"ref": "R1", "lib_id": "Device:R"},
             {"ref": "R2", "lib_id": "Device:R", "in_bom": False}]
    assert snb.apply_assembly_flags(api, comps) == []
    assert len(sch.updates) == 1                               # one UpdateItems round trip
    sent = {(i.reference, i.unit): (i.attributes.do_not_populate,
                                    i.attributes.exclude_from_bill_of_materials,
                                    items.lib_id_of(i))
            for i in sch.updates[0]}
    # each sent unit points at its library again, not at its private copy
    assert sent == {("U1", 1): (True, False, "Amplifier_Operational:LM358"),
                    ("U1", 2): (True, False, "Amplifier_Operational:LM358"),
                    ("R2", 1): (False, True, "Device:R")}
    assert api.reads == [1]                                    # the API object re-reads
    assert snb.apply_assembly_flags(api, comps[1:2]) == [] and len(sch.updates) == 1


@requires_kipy
def test_apply_assembly_flags_warns_when_kicad_ignores_the_field():
    sch = _FakeSchematic([("R1", 1)], apply=False)
    warnings = snb.apply_assembly_flags(_fake_api(sch),
                                        [{"ref": "R1", "lib_id": "Device:R", "dnp": True}])
    assert warnings == ["R1 unit 1: KiCad did not apply do_not_populate (dnp); set it in the "
                        "schematic editor"]


@requires_kipy
def test_apply_assembly_flags_warns_when_kipy_lacks_the_field(monkeypatch: pytest.MonkeyPatch):
    class OldAttributes:                                       # a kipy before the DNP field
        exclude_from_bill_of_materials = property(lambda self: False)
    monkeypatch.setattr(snb.kb, "st", SimpleNamespace(
        SchematicSymbolAttributes=OldAttributes,
        SchematicSymbolInstance=kb.st.SchematicSymbolInstance))
    sch = _FakeSchematic([("R1", 1)])
    warnings = snb.apply_assembly_flags(_fake_api(sch),
                                        [{"ref": "R1", "lib_id": "Device:R", "dnp": True}])
    assert warnings == ["R1: this kipy has no SchematicSymbolAttributes.do_not_populate; "
                        "dnp is not set in the schematic (set it in the schematic editor)"]
    assert sch.updates == []


def _stock_flags_design() -> dict:
    """examples/lm358_buffer with C1 dnp, C2 out of the BOM, a board-only test
    point on GND and a board-only hole without lib_id."""
    design = load_example("lm358_buffer")
    comps = [{**c, **({"dnp": True} if c["ref"] == "C1" else {}),
              **({"in_bom": False} if c["ref"] == "C2" else {})} for c in design["components"]]
    comps += [{"ref": "TP1", "lib_id": "Connector:TestPoint", "value": "GND",
               "footprint": "TestPoint:TestPoint_Pad_D1.0mm", "board_only": True},
              {"ref": "H1", "footprint": "MountingHole:MountingHole_3.2mm_M3",
               "board_only": True}]
    return {**design, "components": comps,
            "nets": {**design["nets"], "GND": design["nets"]["GND"] + [["TP1", "1"]]}}


@pytest.fixture(scope="module")
def api_session():
    from backends import sch_session as sess
    from backends.sch_api import SchematicApi
    if not SchematicApi.available():
        pytest.skip(SchematicApi.unavailable_reason() or "API backend unavailable")
    live = sess.KiCadSession.headless()
    live.load_libraries()
    yield live
    live.close()


def test_api_backend_skips_board_only_parts_and_sets_the_flags(api_session, tmp_path: Path):
    from backends.sch_erc import run_erc
    out = tmp_path / "api_flags.kicad_sch"
    result = snb.build_from_netlist(_stock_flags_design(), str(out), session=api_session)
    assert result["ok"] is True, result
    assert (result["components"], result["board_only"], result["warnings"]) == (
        4, ["TP1", "H1"], [])
    text = out.read_text(encoding="utf-8")
    placed = _placements(text)
    assert not {"TP1", "H1"} & set(placed)
    assert [(p["in_bom"], p["dnp"]) for p in placed["C1"]] == [("yes", "yes")]
    assert [(p["in_bom"], p["dnp"]) for p in placed["C2"]] == [("no", "no")]
    assert all((p["in_bom"], p["dnp"]) == ("yes", "no") for p in placed["U1"])
    assert _labels(text).count("GND") == 4 + 1                 # no TP1 label
    # every unit keeps its library link (a dnp update must not unlink a private copy)
    assert all(":" in p["lib_id"] for units in placed.values() for p in units), placed
    erc = run_erc(str(out))
    assert erc["ok"] is True and erc["violations"] == 0, erc
    all_sev = tc.erc_json(str(out), str(tmp_path / "all_severities.json"), severity_error=False)
    assert "erc_error" not in all_sev, all_sev
    assert [v for s in all_sev["report"]["sheets"] for v in s["violations"]
            if v["type"].startswith("lib_symbol")] == []


# --- design_report: board_only, generalised grounds, the "pins" fallback ----------------

MIXED_MCU = {"name": "MixedMCU", "reference": "U", "pins": [
    {"number": "1", "name": "VDD", "type": "power_in"},
    {"number": "2", "name": "VSS", "type": "power_in"},
    {"number": "3", "name": "AVDD", "type": "power_in"},
    {"number": "4", "name": "AGND", "type": "power_in"},
    {"number": "5", "name": "PA0", "type": "bidirectional"},
    {"number": "6", "name": "PGND", "type": "power_in"},
    {"number": "7", "name": "GND", "type": "power_in"}]}
CAP = {"name": "Cap", "reference": "C", "pins": [
    {"number": "1", "name": "~", "type": "passive"},
    {"number": "2", "name": "~", "type": "passive"}]}


@pytest.fixture
def mix_lib(tmp_path: Path) -> Path:
    import sym_author
    for spec in (MIXED_MCU, CAP):
        sym_author.author_symbol(spec, tmp_path / "MixLib.kicad_sym")
    return tmp_path


def _vss_design() -> dict:
    """Caps to VSS and AGND; a ground pad on "0V" and a GND-named pin on "RTN"
    (a net only its pin calls ground), neither with a cap on it."""
    return {"name": "vss", "components": [
        {"ref": "U1", "lib_id": "MixLib:MixedMCU", "value": "MCU", "footprint": "F"},
        {"ref": "C1", "lib_id": "MixLib:Cap", "value": "100n", "footprint": "C"},
        {"ref": "C2", "lib_id": "MixLib:Cap", "value": "100n", "footprint": "C"}],
        "nets": {"+3V3": [["U1", "1"], ["C1", "1"]], "VSS": [["U1", "2"], ["C1", "2"]],
                 "AVDD": [["U1", "3"], ["C2", "1"]], "AGND": [["U1", "4"], ["C2", "2"]],
                 "0V": [["U1", "6"]], "RTN": [["U1", "7"]]},
        "no_connect": [["U1", "5"]]}


def test_vss_agnd_design_finds_its_bypass_caps(mix_lib: Path):
    rep = design_report.analyze(_vss_design(), str(mix_lib))
    assert rep["errors"] == []
    assert rep["decoupling"] == {"checked": [
        {"ref": "U1", "rails": ["+3V3", "0V", "AGND", "AVDD", "RTN", "VSS"],
         "decoupled": ["+3V3", "AGND", "AVDD", "VSS"]}], "uncovered": []}
    assert rep["ground_nets"] == ["0V", "AGND", "RTN", "VSS"]
    assert "[WARN]" not in design_report.to_markdown(rep)


def test_a_rail_without_a_cap_is_still_uncovered(mix_lib: Path):
    design = _vss_design()
    design = {**design, "components": [c for c in design["components"] if c["ref"] != "C2"],
              "nets": {**design["nets"], "AVDD": [["U1", "3"]], "AGND": [["U1", "4"]]}}
    rep = design_report.analyze(design, str(mix_lib))
    assert rep["decoupling"]["uncovered"] == [{"ref": "U1", "rails_without_cap": ["AVDD"]}]


@pytest.mark.parametrize("name", ["GND", "gnd", "AGND", "DGND", "PGND", "SGND", "CGND",
                                  "GND_ISO", "GNDA", "VSS", "Vss", "VSSA", "VSS_IO", "0V",
                                  "EARTH", "earth"])
def test_ground_names(name: str):
    assert design_report.is_ground_name(name) is True


@pytest.mark.parametrize("name", ["VCC", "BGND", "XGND", "GROUND", "0V5", "EARTH1", "AVSS",
                                  "SIGNAL_GND", "", "V-"])
def test_not_ground_names(name: str):
    assert design_report.is_ground_name(name) is False


def test_board_only_parts_are_left_out_of_the_pin_checks():
    """TP1's lib_id resolves nowhere and H1 has none: neither is an error; a
    board-only part that does resolve is never floating; a board-only "cap"
    pad decouples nothing."""
    design = {"name": "bo", "components": [
        {"ref": "U1", "lib_id": "TestLib:DerivedAmp", "value": "amp", "footprint": "F"},
        {"ref": "C1", "lib_id": "TestLib:PosFileR", "value": "100n", "footprint": "F"},
        {"ref": "C9", "lib_id": "TestLib:PosFileR", "value": "pad", "footprint": "F",
         "board_only": True},
        {"ref": "TP1", "lib_id": "Nope:TP", "footprint": "TP", "board_only": True},
        {"ref": "TP2", "lib_id": "TestLib:PosFileR", "footprint": "TP", "board_only": True},
        {"ref": "H1", "footprint": "MountingHole:MountingHole_3.2mm_M3", "board_only": True}],
        "nets": {"VCC": [["U1", "8"], ["C9", "1"]],
                 "GND": [["U1", "4"], ["C1", "2"], ["C9", "2"], ["TP1", "1"]],
                 "OUT": [["U1", "1"], ["U1", "2"]], "SPARE": [["C1", "1"]]},
        "no_connect": [["U1", "3"], ["U1", "5"], ["U1", "6"], ["U1", "7"]]}
    rep = design_report.analyze(design, str(FIXTURE_SYMBOLS))
    assert rep["errors"] == []
    assert rep["flags"]["floating_pins"] == []                 # TP2's two pads not judged
    assert rep["decoupling"]["uncovered"] == [{"ref": "U1", "rails_without_cap": ["VCC"]}]


PINS = {"1": {"name": "VDD", "type": "power_in"}, "2": {"name": "VSS", "type": "power_in"},
        "3": {"name": "IO", "type": "bidirectional"}, "4": {"name": "~", "type": "passive"}}


def _pins_design(lib_id: str | None = "Vendor:Missing", pins: object = PINS) -> dict:
    u2 = {"ref": "U2", "value": "chip", "footprint": "Package_SO:SOIC-8", "pins": pins}
    if lib_id:
        u2["lib_id"] = lib_id
    return {"name": "pins", "components": [
        u2, {"ref": "C1", "lib_id": "TestLib:PosFileR", "value": "100n", "footprint": "C"}],
        "nets": {"VCC": [["U2", "VDD"], ["C1", "1"]], "GND_ISO": [["U2", "2"], ["C1", "2"]]}}


@pytest.mark.parametrize("lib_id", ["Vendor:Missing", None], ids=["unresolvable", "no-lib-id"])
def test_pins_fallback_replaces_an_unreadable_symbol(lib_id):
    rep = design_report.analyze(_pins_design(lib_id), str(FIXTURE_SYMBOLS))
    assert rep["errors"] == [] and rep["pins_fallback"] == ["U2"]
    assert rep["decoupling"]["checked"] == [
        {"ref": "U2", "rails": ["GND_ISO", "VCC"], "decoupled": ["GND_ISO", "VCC"]}]
    # F5-08's floating_detail: names and types from the pins map, no stack hint
    assert rep["flags"]["floating_detail"] == [
        {"pin": "U2.3", "ref": "U2", "pad": "3", "type": "bidirectional", "name": "IO",
         "same_name_net": None, "lib_id": lib_id, "footprint": "Package_SO:SOIC-8"},
        {"pin": "U2.4", "ref": "U2", "pad": "4", "type": "passive", "name": None,
         "same_name_net": None, "lib_id": lib_id, "footprint": "Package_SO:SOIC-8"}]
    assert rep["power_tree"]["VCC"]["drivers"] == ["(passive/flagged source)"]
    md = design_report.to_markdown(rep)
    assert "- U2: symbol unavailable; pin types taken from its \"pins\" map" in md


def test_a_readable_symbol_wins_over_the_pins_map():
    design = _pins_design("TestLib:PosFileR", {"1": {"name": "X", "type": "power_in"}})
    rep = design_report.analyze(design, str(FIXTURE_SYMBOLS))
    assert rep["pins_fallback"] == [] and rep["decoupling"]["checked"] == []


@pytest.mark.parametrize("pins,why", [
    ([], "must be an object of pad -> {name, type}"),
    ({}, "must be an object of pad -> {name, type}"),
    ({"1": "power_in"}, "pad '1' must be an object with a 'type'"),
    ({"1": {"name": "VDD", "type": "power"}}, "pad '1' has unknown type 'power'"),
    ({"1": {"name": 5, "type": "passive"}}, "pad '1' name must be a string"),
])
def test_a_malformed_pins_map_is_reported(pins, why):
    rep = design_report.analyze(_pins_design(pins=pins), str(FIXTURE_SYMBOLS))
    assert rep["pins_fallback"] == []
    assert len(rep["errors"]) == 1
    assert rep["errors"][0].startswith("pin types unavailable for Vendor:Missing (U2): "
                                       "SymbolLibError: ")
    assert f"its \"pins\" map is unusable: {why}" in rep["errors"][0]


def test_refs_without_pins_still_report_the_unreadable_symbol():
    design = _pins_design()
    design = {**design, "components": design["components"] + [
        {"ref": "U3", "lib_id": "Vendor:Missing", "value": "chip", "footprint": "F"}]}
    rep = design_report.analyze(design, str(FIXTURE_SYMBOLS))
    assert rep["pins_fallback"] == ["U2"]
    assert [e.split(": ", 1)[0] for e in rep["errors"]] == [
        "pin types unavailable for Vendor:Missing (U3)"]


def test_no_symbol_source_still_skips_the_pin_checks():
    rep = design_report.analyze(_pins_design(), "")
    assert rep["symbols_available"] is False and rep["pins_fallback"] == []
    assert rep["errors"] == [] and rep["decoupling"]["checked"] == []
