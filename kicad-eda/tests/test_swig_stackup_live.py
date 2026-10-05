"""swig_stackup (ADR D7.5) and what KiCad makes of the spliced stackup.

venv tier (always runs): stackup_from_dict, load_or_resolve (no / fresh / stale /
unusable artifact, KICAD_EDA_STACKUP, the board-only lookup), zone_problems.

live tier (KiCad 9.0.4 locally, 10.0.5 under the K10 recipe; skipped without a
pcbnew Python): tests/fixtures/stackup_kicad/build_board.py builds five boards
(2L B mixed, 6L JLC06161H-2116, 8L default, the ADR D7.2.2 4L hybrid with
0.03048 mm masks, 2L without mask) through apply_layers + finalize; kicad-cli
DRC, Gerbers + job file and IPC-2581 then check them. The 9.0.4 hybrid job file
and copper Gerber headers are committed under tests/fixtures/stackup_kicad/
k9_4l_hybrid (D7-22 reads them; regenerate with KICAD_EDA_REGEN_STACKUP_FIXTURES=1).
"""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

import stackup_kicad as sk
import stackup_model as sm
import swig_stackup as ss
import toolchain
from fixtures.swig_python import TOOLS, kicad_footprints, require_kicad_python, run_child

FIXTURES = Path(__file__).resolve().parent / "fixtures"
STACKUPS = FIXTURES / "stackups"
HERE = FIXTURES / "stackup_kicad"
K9_HYBRID = HERE / "k9_4l_hybrid"
REGEN = os.environ.get("KICAD_EDA_REGEN_STACKUP_FIXTURES") == "1"
ERROR_LINE = re.compile(r"(^|\s)Error:", re.M)
BOARDS = {"2l_mixed": ("d722_2l_mixed", {}), "6l_2116": ("d722_6l_2116", {}),
          "8l_default": ("default_8l", {}),
          "4l_hybrid": ("d722_4l_hybrid", {"mask": {"over_substrate": 0.03048}}),
          "2l_nomask": ("d722_2l_mixed", {"mask": None})}
LEGACY = "4l_legacy"                 # built too, but it carries no stackup block
COPPER_SUFFIX = {"F.Cu": "F_Cu.gtl", "In1.Cu": "In1_Cu.g1", "In2.Cu": "In2_Cu.g2",
                 "B.Cu": "B_Cu.gbl"}


def _design(fixture: str, changes: dict | None = None) -> dict:
    design = json.loads((STACKUPS / f"{fixture}.json").read_text(encoding="utf-8"))["design"]
    if changes:
        design["stackup"] = {**design.get("stackup", {}), **changes}
    return design


def _rs(design: dict) -> sm.ResolvedStackup:
    rs, issues = sm.resolve(design)
    assert rs is not None, issues
    return rs


ALL = sorted(p.stem for p in STACKUPS.glob("*.json"))
RESOLVED = [n for n in ALL if sm.resolve(_design(n))[0] is not None]


def _artifact(path: Path, stackup: dict, schema: str = ss.ARTIFACT_SCHEMA) -> Path:
    path.write_text(json.dumps({"schema": schema, "stackup": stackup}), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# venv tier
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", RESOLVED)
def test_stackup_from_dict_round_trips_every_fixture(name):
    rs = _rs(_design(name))
    back = ss.stackup_from_dict(json.loads(json.dumps(rs.to_dict())))
    assert back.to_dict() == rs.to_dict()
    if rs.legacy is None:
        assert sk.emit(back) == sk.emit(rs)


def test_stackup_from_dict_refuses_a_foreign_dict():
    data = _rs(_design("d722_4l_hybrid")).to_dict()
    odd_copper = [{**data["copper"][0], "colour": "red"}, *data["copper"][1:]]
    for broken in ({k: v for k, v in data.items() if k != "mask"},
                   {**data, "kicad_copper_count": 6}, {**data, "copper": odd_copper}):
        with pytest.raises(ValueError, match="stackup"):
            ss.stackup_from_dict(broken)


def test_load_or_resolve_without_an_artifact_resolves_the_netlist(monkeypatch):
    monkeypatch.delenv(ss.ARTIFACT_ENV, raising=False)
    design = _design("d722_6l_2116")
    before = copy.deepcopy(design)
    loaded = ss.load_or_resolve(design)
    assert (loaded.source, loaded.artifact, loaded.note) == ("resolved", None, None)
    assert loaded.stackup.to_dict() == _rs(design).to_dict()
    assert len(loaded.stackup.decision_drafts) == 2 and design == before


def test_load_or_resolve_uses_a_fresh_artifact_and_flags_the_rest(tmp_path, monkeypatch):
    monkeypatch.delenv(ss.ARTIFACT_ENV, raising=False)
    design = _design("d722_4l_hybrid")
    rs = _rs(design)
    fresh = ss.load_or_resolve(design, _artifact(tmp_path / "a.stackup.json", rs.to_dict()))
    assert (fresh.source, fresh.note) == ("artifact", None)
    assert fresh.stackup.to_dict() == rs.to_dict()
    stale = _artifact(tmp_path / "b.stackup.json", _rs(_design("d722_6l_2116")).to_dict())
    garbage = tmp_path / "d.stackup.json"
    garbage.write_text("{not json", encoding="utf-8")
    cases = [("stale", stale),
             ("unusable", _artifact(tmp_path / "c.stackup.json", rs.to_dict(), schema="x@1")),
             ("unusable", garbage),
             ("no stackup artifact", tmp_path / "missing.stackup.json")]
    for word, path in cases:
        loaded = ss.load_or_resolve(design, path)
        assert loaded.source == "resolved" and word in loaded.note, (word, loaded.note)
        assert loaded.stackup.to_dict() == rs.to_dict()
    monkeypatch.setenv(ss.ARTIFACT_ENV, str(stale))
    assert "stale" in ss.load_or_resolve(design).note


def test_load_or_resolve_board_only_lookup(tmp_path, monkeypatch):
    monkeypatch.delenv(ss.ARTIFACT_ENV, raising=False)
    rs = _rs(_design("default_8l"))
    loaded = ss.load_or_resolve(None, _artifact(tmp_path / "b.stackup.json", rs.to_dict()))
    assert loaded.source == "artifact" and loaded.stackup.to_dict() == rs.to_dict()
    with pytest.raises(ValueError, match="artifact"):
        ss.load_or_resolve(None)
    with pytest.raises(ValueError, match="ST-E1"):
        ss.load_or_resolve(_design("st_e1"))


def _zone(layer: str, net: str, **extra: object) -> dict:
    return {"name": "", "net": net, "layer": layer, "layers": [layer], "rule_area": False,
            "filled": True, **extra}


def test_zone_problems_follow_the_roles():
    rs = _rs(_design("d722_4l_hybrid"))           # In1 plane GND, In2 plane +3V3
    good = [_zone("In1.Cu", "GND"), _zone("In2.Cu", "+3V3"), _zone("F.Cu", "GND")]
    assert ss.zone_problems(rs, good) == []
    keepout = _zone("F.Cu", "", rule_area=True, layers=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    assert ss.zone_problems(rs, good + [keepout]) == []
    foreign = ss.zone_problems(rs, good + [_zone("In1.Cu", "+5V")])
    assert len(foreign) == 1 and "+5V" in foreign[0] and "In1.Cu" in foreign[0]
    missing = ss.zone_problems(rs, good[:1])
    assert len(missing) == 1 and "In2.Cu" in missing[0] and "+3V3" in missing[0]
    assert ss.zone_problems(rs, good[:1], require_planes=False) == []
    single = _rs(_design("single_sided"))
    unused = ss.zone_problems(single, [_zone("B.Cu", "GND")])
    assert len(unused) == 1 and "unused" in unused[0]
    mixed = _rs(_design("d722_2l_mixed"))
    assert ss.zone_problems(mixed, [_zone("B.Cu", "GND"), _zone("B.Cu", "+3V3")]) == []


def test_apply_layers_needs_pcbnew():
    if ss.pcbnew is not None:
        pytest.skip("pcbnew is importable here; the live tier covers apply_layers")
    with pytest.raises(RuntimeError, match="pcbnew"):
        ss.apply_layers(object(), _rs(_design("d722_2l_mixed")))


# ---------------------------------------------------------------------------
# live tier
# ---------------------------------------------------------------------------
def _cli(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL, timeout=timeout)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0 and not ERROR_LINE.search(output), (args, output[-2000:])
    return proc


def _fab(cli: str, pcb: Path, ipc: bool) -> dict:
    report = pcb.with_name(pcb.stem + "_drc.json")
    report.unlink(missing_ok=True)
    _cli([cli, "pcb", "drc", "--format", "json", "--severity-error", "-o", str(report),
          str(pcb)])
    gerbers = pcb.parent / f"{pcb.stem}_gerbers"
    gerbers.mkdir(exist_ok=True)
    _cli([cli, "pcb", "export", "gerbers", "-o", str(gerbers) + os.sep, str(pcb)])
    out = {"drc": json.loads(report.read_text(encoding="utf-8")), "gerbers": gerbers,
           "job": json.loads((gerbers / f"{pcb.stem}-job.gbrjob").read_text(encoding="utf-8"))}
    if ipc:
        xml = pcb.with_name(pcb.stem + "_ipc2581.xml")
        _cli([cli, "pcb", "export", "ipc2581", "-o", str(xml), str(pcb)])
        out["ipc"] = xml.read_text(encoding="utf-8", errors="replace")
    return out


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> dict:
    kpy = require_kicad_python()
    work = tmp_path_factory.mktemp("stackup_live")
    specs = [{"name": name, "design": _design(*spec)} for name, spec in BOARDS.items()]
    specs.append({"name": LEGACY, "design": _design("legacy")})
    (work / "boards.json").write_text(json.dumps(specs), encoding="utf-8")
    script = (HERE / "build_board.py").read_text(encoding="utf-8")
    fp_dir = kicad_footprints(kpy, "Connector_PinHeader_2.54mm.pretty")
    res = run_child(script, [work / "boards.json", work, fp_dir, TOOLS], work, timeout=900)
    errors = {n: b["error"] for n, b in res["boards"].items() if "error" in b}
    assert not errors, errors
    cli = toolchain.board_cli(res["boards"]["2l_mixed"]["pcb"])
    if not cli:
        pytest.skip("no kicad-cli for the built boards")
    fab = {n: _fab(cli, Path(b["pcb"]), n == "4l_hybrid") for n, b in res["boards"].items()}
    return {**res, "fab": fab, "rs": {s["name"]: _rs(s["design"]) for s in specs}}


@pytest.mark.parametrize("name", BOARDS)
def test_finalize_verifies_every_board(live, name):
    result = live["boards"][name]["finalize"]
    assert result["ok"] and result["problems"] == [] and result["spliced"], result


@pytest.mark.parametrize("name", BOARDS)
def test_copper_types_and_thickness_persist(live, name):
    board, rs = live["boards"][name], live["rs"][name]
    assert board["copper"] == list(rs.copper_names())
    assert board["layer_types"] == {c.name: live["lt"][c.kicad_type] for c in rs.copper}
    assert board["has_stackup"]
    assert board["thickness_mm"] == pytest.approx(rs.board_thickness_computed, abs=1e-6)
    text = Path(board["pcb"]).read_text(encoding="utf-8")
    assert sk.copper_types(text) == {c.name: c.kicad_type for c in rs.copper}
    assert re.search(r"\(version (\d+)\)", text).group(1) in ("20241229", "20260206")


def test_the_legacy_shorthand_finalizes_without_a_block(live):
    board, rs = live["boards"][LEGACY], live["rs"][LEGACY]
    assert board["finalize"]["ok"] and not board["finalize"]["spliced"], board["finalize"]
    text = Path(board["pcb"]).read_text(encoding="utf-8")
    assert sk.extract(text) is None
    assert board["thickness_mm"] == pytest.approx(rs.board_thickness_computed, abs=1e-6)
    assert sk.copper_types(text) == {c.name: c.kicad_type for c in rs.copper}
    drc = live["fab"][LEGACY]["drc"]
    assert drc["violations"] == [] and drc["unconnected_items"] == [], drc


def test_power_and_mixed_layer_types_are_exercised(live):
    kinds = {c.kicad_type for rs in live["rs"].values() for c in rs.copper}
    assert {"power", "mixed", "signal"} <= kinds


@pytest.mark.parametrize("name", BOARDS)
def test_the_block_is_stable_over_a_second_save(live, name):
    board, rs = live["boards"][name], live["rs"][name]
    for path in (board["pcb"], board["rt"]):
        saved = sk.extract(Path(path).read_text(encoding="utf-8"))
        assert saved is not None and sk.normalize(saved) == sk.normalize(sk.emit(rs)), path


@pytest.mark.parametrize("name", BOARDS)
def test_zones_follow_the_roles(live, name):
    zones, rs = live["boards"][name]["finalize"]["zones"], live["rs"][name]
    pours = {(z["layer"], z["net"]) for z in zones if not z["rule_area"]}
    assert pours == {(c.name, c.net) for c in rs.copper if c.role in ("plane", "mixed")}
    assert all(z["filled"] and z["layers"] == [z["layer"]] for z in zones)


@pytest.mark.parametrize("name", BOARDS)
def test_drc_is_clean(live, name):
    drc = live["fab"][name]["drc"]
    assert drc["violations"] == [] and drc["unconnected_items"] == [], drc


def _rows(rs: sm.ResolvedStackup) -> int:
    return len(rs.copper) + sum(len(d.plies) for d in rs.dielectrics) + 2


@pytest.mark.parametrize("name", BOARDS)
def test_the_job_file_carries_the_stackup(live, name):
    job, rs = live["fab"][name]["job"], live["rs"][name]
    rows = [r for r in job["MaterialStackup"] if "Thickness" in r]
    assert len(rows) == _rows(rs)
    specs = job["GeneralSpecs"]
    assert specs["LayerNumber"] == rs.kicad_copper_count
    assert specs["BoardThickness"] == round(rs.board_thickness_computed, 4)
    assert specs.get("ImpedanceControlled", False) is rs.impedance_controlled
    copper = [r["Thickness"] for r in rows if r["Type"] == "Copper"]
    assert copper == [round(c.thickness, 4) for c in rs.copper]
    masks = [r["Thickness"] for r in rows if r["Type"] == "SolderMask"]
    assert masks == [round(rs.mask.over_substrate, 4)] * 2


def test_acceptance_numbers(live):
    hybrid, six = live["fab"]["4l_hybrid"]["job"], live["fab"]["6l_2116"]["job"]
    for job, rows, thickness in ((hybrid, 10, 1.3685), (six, 16, 1.6554)):
        assert sum("Thickness" in r for r in job["MaterialStackup"]) == rows
        assert job["GeneralSpecs"]["BoardThickness"] == thickness
        assert job["GeneralSpecs"]["ImpedanceControlled"] is True
    dk = {(r["Material"], r["DielectricConstant"]) for r in hybrid["MaterialStackup"]
          if r["Type"] == "Dielectric"}
    assert ("RO4350B", "3.66") in dk


def _version(job: dict) -> tuple[int, ...]:
    text = job["Header"]["GenerationSoftware"]["Version"]
    return tuple(int(x) for x in re.findall(r"\d+", text)[:3])


def _inner_numbers(job: dict) -> list[str]:
    return [f["FileFunction"] for f in job["FilesAttributes"]
            if f["FileFunction"].startswith("Copper,") and f["FileFunction"].endswith(",Inr")]


@pytest.mark.parametrize("name", ["4l_hybrid", "6l_2116", "8l_default"])
def test_job_file_inner_copper_numbering_pins_the_904_bug(live, name):
    job, n = live["fab"][name]["job"], live["rs"][name].kicad_copper_count
    buggy = _version(job) < (9, 0, 5)                # fixed upstream in 05c11d02 (9.0.5)
    want = [f"Copper,L{2 * k + 3 if buggy else k + 1},Inr" for k in range(1, n - 1)]
    assert _inner_numbers(job) == want
    gerbers = live["fab"][name]["gerbers"]
    for k in range(1, n - 1):
        head = (gerbers / f"{name}-In{k}_Cu.g{k}").read_text(encoding="utf-8")
        assert f"%TF.FileFunction,Copper,L{k + 1},Inr*%" in head


def test_ipc2581_names_the_rogers_core(live):
    ipc = live["fab"]["4l_hybrid"]["ipc"]
    assert "RO4350B" in ipc and "3.66" in ipc


@pytest.mark.parametrize("name", BOARDS)
def test_the_import_reader_reads_kicads_board_back(live, name):
    rs, text = live["rs"][name], Path(live["boards"][name]["rt"]).read_text(encoding="utf-8")
    stackup = sk.netlist_stackup_from_board(text)
    design = _design(*BOARDS[name])
    rs2, issues = sm.resolve({**design, "stackup": stackup})
    assert rs2 is not None and not [i for i in issues if i.severity == "error"], issues
    assert sk.emit(rs2) == sk.emit(rs)
    assert [(c.name, c.role, c.net) for c in rs2.copper] == \
        [(c.name, c.role, c.net) for c in rs.copper]


def _gerber_header(text: str) -> str:
    """The attribute / comment header of a Gerber (up to the aperture list)."""
    head = []
    for line in text.splitlines():
        if line.startswith("%AD") or line.startswith("G04 APERTURE LIST"):
            break
        head.append(line)
    return "\n".join(head + ["M02*"]) + "\n"


def _renumbered(attributes: list[dict]) -> list[dict]:
    """Copper FilesAttributes with 9.0.4's inner numbering (In_k = L(2k+3))
    corrected to the layer position (In_k = L(k+1)), as 9.0.5+ and 10 write it."""
    def fix(entry: dict) -> dict:
        m = re.fullmatch(r"Copper,L(\d+),Inr", entry["FileFunction"])
        return {**entry, "FileFunction": f"Copper,L{(int(m.group(1)) - 3) // 2 + 1},Inr"} \
            if m else entry
    return [fix(e) for e in attributes if e["FileFunction"].startswith("Copper,")]


def test_k9_hybrid_fixture_matches_the_live_job_file(live):
    job = live["fab"]["4l_hybrid"]["job"]
    if _version(job) != (9, 0, 4):           # KiCad 10: same rows, inner copper by position
        committed = json.loads((K9_HYBRID / "4l_hybrid-job.gbrjob").read_text(encoding="utf-8"))
        assert job["MaterialStackup"] == committed["MaterialStackup"]
        assert [e for e in job["FilesAttributes"] if e["FileFunction"].startswith("Copper,")] \
            == _renumbered(committed["FilesAttributes"])
        return
    _check_904_fixture(live)


def _check_904_fixture(live: dict) -> None:
    gerbers = live["fab"]["4l_hybrid"]["gerbers"]
    names = ["4l_hybrid-job.gbrjob"] + [f"4l_hybrid-{s}" for s in COPPER_SUFFIX.values()]
    fresh = {"4l_hybrid-job.gbrjob": (gerbers / names[0]).read_text(encoding="utf-8")}
    fresh |= {n: _gerber_header((gerbers / n).read_text(encoding="utf-8")) for n in names[1:]}
    if REGEN:
        K9_HYBRID.mkdir(parents=True, exist_ok=True)
        for n, text in fresh.items():
            (K9_HYBRID / n).write_text(text, encoding="utf-8", newline="\n")
    committed = json.loads((K9_HYBRID / names[0]).read_text(encoding="utf-8"))
    for key in ("MaterialStackup", "FilesAttributes", "DesignRules"):
        assert committed[key] == json.loads(fresh[names[0]])[key], key
    for n in names[1:]:
        tf = [ln for ln in fresh[n].splitlines() if ln.startswith("%TF.FileFunction")]
        assert tf and tf == [ln for ln in (K9_HYBRID / n).read_text(encoding="utf-8").splitlines()
                             if ln.startswith("%TF.FileFunction")]


def test_k9_hybrid_fixture_content():
    """The committed 9.0.4 job file shows the D7.1 facts D7-22 builds on."""
    job = json.loads((K9_HYBRID / "4l_hybrid-job.gbrjob").read_text(encoding="utf-8"))
    assert _version(job) == (9, 0, 4)
    assert sum("Thickness" in r for r in job["MaterialStackup"]) == 10
    assert job["GeneralSpecs"]["BoardThickness"] == 1.3685
    assert job["GeneralSpecs"]["ImpedanceControlled"] is True
    assert _inner_numbers(job) == ["Copper,L5,Inr", "Copper,L7,Inr"]
    for layer, suffix in COPPER_SUFFIX.items():
        head = (K9_HYBRID / f"4l_hybrid-{suffix}").read_text(encoding="utf-8")
        position = {"F.Cu": "L1,Top", "In1.Cu": "L2,Inr", "In2.Cu": "L3,Inr", "B.Cu": "L4,Bot"}
        assert f"%TF.FileFunction,Copper,{position[layer]}*%" in head
