"""fab_stackup: the G5 fab outputs that carry the stackup (ADR D7.12, R24).

Fixtures (tests/fixtures/fab_stackup/, copied from the D7 planning pass):
* k9_6l/: the KiCad 9.0.4 job file and copper Gerber headers of the 6L probe board s6L
  (inner copper numbered L5/L7/L9/L11 in the job file, L2..L5 in each Gerber's %TF);
  s6l_k9.json is its stackup in the D7.2.2 layers form (14 thickness rows).
* k10_6l_hybrid/: the KiCad 10.0.5 job file and copper headers of s6L_hybrid
  (tests/fixtures/stackups/s6l_hybrid.json; 15 rows, numbering already right).
* D7-03's tests/fixtures/stackup_kicad/k9_4l_hybrid/ (9.0.4, the ADR 4L hybrid, 10 rows).

Live tier (KiCad 9.0.4 locally, skipped without a pcbnew Python): D7-03's committed
builder makes the JLC06161H-2116 6L board and the 2L board without mask; kicad-cli
exports Gerbers (and IPC-2581); the job file is fixed and checked here.
"""
from __future__ import annotations

import copy
import csv
import io
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

import fab_stackup as fs
import netlist_schema
import physics_resolve as pr
import stackup_kicad as sk
import stackup_model as sm
import toolchain
from fixtures.swig_python import TOOLS, kicad_footprints, require_kicad_python, run_child

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HERE = FIXTURES / "fab_stackup"
STACKUPS = FIXTURES / "stackups"
K9_6L = HERE / "k9_6l"
K10 = HERE / "k10_6l_hybrid"
K9_4L = FIXTURES / "stackup_kicad" / "k9_4l_hybrid"
JOBS = {"k9_6l": K9_6L / "s6L-job.gbrjob", "k10": K10 / "k10-job.gbrjob",
        "k9_4l": K9_4L / "4l_hybrid-job.gbrjob"}
ERROR_LINE = re.compile(r"(^|\s)Error:", re.M)


def _design(name: str) -> dict:
    path = HERE / f"{name}.json" if (HERE / f"{name}.json").exists() else STACKUPS / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))["design"]


def _rs(design: dict) -> sm.ResolvedStackup:
    rs, issues = sm.resolve(design)
    assert rs is not None, issues
    return rs


def _artifact(design: dict, tmp_path: Path) -> dict:
    return pr.resolve_design(design, tmp_path / "netlist.json", fd="off")


def _copy(src: Path, dst: Path, newline: str | None = "\n") -> Path:
    """Copy a fixture folder with LF or CRLF line ends, or byte for byte (newline None:
    whatever the checkout gave it; core.autocrlf=true makes the fixtures CRLF on Windows)."""
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        data = f.read_bytes()
        if newline is not None:
            data = data.replace(b"\r\n", b"\n").replace(b"\n", newline.encode("ascii"))
        (dst / f.name).write_bytes(data)
    return dst


def _job(path: Path) -> dict:
    return json.loads(path.read_bytes().decode("utf-8"))


def _copper_functions(job: dict) -> list[str]:
    return [e["FileFunction"] for e in job["FilesAttributes"]
            if e["FileFunction"].startswith("Copper,")]


def _fixed(src: Path, tmp_path: Path, name: str) -> Path:
    work = _copy(src, tmp_path / name)
    job = next(work.glob("*.gbrjob"))
    result = fs.fix_gbrjob_file_attributes(job)
    assert result["problems"] == [], result
    return job


# ---------------------------------------------------------------------------
# fix_gbrjob_file_attributes
# ---------------------------------------------------------------------------
def test_fix_renumbers_the_904_6l_inner_copper(tmp_path):
    work = _copy(K9_6L, tmp_path / "k9")
    job = work / "s6L-job.gbrjob"
    before = job.read_bytes()
    result = fs.fix_gbrjob_file_attributes(job, work)
    assert result["written"] and result["problems"] == []
    assert [(c["from"], c["to"]) for c in result["changed"]] == [
        ("Copper,L5,Inr", "Copper,L2,Inr"), ("Copper,L7,Inr", "Copper,L3,Inr"),
        ("Copper,L9,Inr", "Copper,L4,Inr"), ("Copper,L11,Inr", "Copper,L5,Inr")]
    assert _copper_functions(_job(job)) == [
        "Copper,L1,Top", "Copper,L2,Inr", "Copper,L3,Inr", "Copper,L4,Inr", "Copper,L5,Inr",
        "Copper,L6,Bot"]
    expected = before
    for old, new in ((b"L5,Inr", b"L2,Inr"), (b"L7,Inr", b"L3,Inr"), (b"L9,Inr", b"L4,Inr"),
                     (b"L11,Inr", b"L5,Inr")):
        expected = expected.replace(old, new, 1)
    assert job.read_bytes() == expected          # nothing else in the file moved


def test_fix_renumbers_the_904_4l_hybrid(tmp_path):
    job = _copy(K9_4L, tmp_path / "h4") / "4l_hybrid-job.gbrjob"
    result = fs.fix_gbrjob_file_attributes(job)          # Gerbers beside the job file
    assert [(c["from"], c["to"]) for c in result["changed"]] == [
        ("Copper,L5,Inr", "Copper,L2,Inr"), ("Copper,L7,Inr", "Copper,L3,Inr")]
    assert _copper_functions(_job(job)) == ["Copper,L1,Top", "Copper,L2,Inr", "Copper,L3,Inr",
                                            "Copper,L4,Bot"]


@pytest.mark.parametrize("newline", [None, "\n", "\r\n"], ids=["as-checked-out", "lf", "crlf"])
def test_fix_is_a_byte_identical_no_op_on_1005(tmp_path, newline):
    job = _copy(K10, tmp_path / "k10", newline) / "k10-job.gbrjob"
    before, stamp = job.read_bytes(), job.stat().st_mtime_ns
    assert newline is None or (b"\r\n" in before) is (newline == "\r\n")
    result = fs.fix_gbrjob_file_attributes(job)
    assert result == {"path": str(job), "changed": [], "problems": [], "written": False}
    assert job.read_bytes() == before and job.stat().st_mtime_ns == stamp
    if newline is None:                       # the shipped fixture itself, untouched
        assert job.read_bytes() == (K10 / "k10-job.gbrjob").read_bytes()


def test_fix_is_idempotent_and_keeps_crlf(tmp_path):
    work = _copy(K9_6L, tmp_path / "crlf", "\r\n")
    job = work / "s6L-job.gbrjob"
    first = fs.fix_gbrjob_file_attributes(job)
    data = job.read_bytes()
    assert first["written"] and data.count(b"\r\n") == data.count(b"\n")
    again = fs.fix_gbrjob_file_attributes(job)
    assert again["changed"] == [] and not again["written"] and job.read_bytes() == data


def test_fix_reports_what_it_cannot_check(tmp_path):
    work = _copy(K9_6L, tmp_path / "gaps")
    (work / "s6L-In2_Cu.g2").unlink()
    (work / "s6L-In3_Cu.g3").write_text("%FSLAX46Y46*%\nM02*\n", encoding="utf-8")
    result = fs.fix_gbrjob_file_attributes(work / "s6L-job.gbrjob")
    assert [c["to"] for c in result["changed"]] == ["Copper,L2,Inr", "Copper,L5,Inr"]
    assert len(result["problems"]) == 2
    assert "s6L-In2_Cu.g2" in result["problems"][0] and "missing" in result["problems"][0]
    assert "s6L-In3_Cu.g3" in result["problems"][1] and "FileFunction" in result["problems"][1]
    funcs = _copper_functions(_job(work / "s6L-job.gbrjob"))
    assert funcs[2:4] == ["Copper,L7,Inr", "Copper,L9,Inr"]      # left as KiCad wrote them


def test_fix_reads_x2_comments_and_leaves_other_files_alone(tmp_path):
    work = _copy(K9_6L, tmp_path / "x2c")
    g1 = work / "s6L-In1_Cu.g1"
    g1.write_text(g1.read_text(encoding="utf-8").replace("%TF.FileFunction,Copper,L2,Inr*%",
                                                         "G04 #@! TF.FileFunction,Copper,L2,Inr*"),
                  encoding="utf-8")
    (work / "s6L-F_Mask.gts").write_text("%TF.FileFunction,Soldermask,Bot*%\n", encoding="utf-8")
    result = fs.fix_gbrjob_file_attributes(work / "s6L-job.gbrjob")
    assert result["changed"][0] == {"path": "s6L-In1_Cu.g1", "from": "Copper,L5,Inr",
                                    "to": "Copper,L2,Inr"}
    assert "SolderMask,Top" in [e["FileFunction"] for e in
                                _job(work / "s6L-job.gbrjob")["FilesAttributes"]]


def test_public_names_exist():
    assert all(hasattr(fs, name) for name in fs.__all__) and fs.GBRJOB_SUFFIX == "-job.gbrjob"


def test_gerber_file_function(tmp_path):
    assert fs.gerber_file_function(K9_6L / "s6L-In4_Cu.g4") == "Copper,L5,Inr"
    assert fs.gerber_file_function(K10 / "k10-B_Cu.gbl") == "Copper,L6,Bot"
    plain = tmp_path / "plain.gbr"
    plain.write_text("G04 no attributes*\n%ADD10C,0.1*%\n%TF.FileFunction,Copper,L9,Inr*%\n",
                     encoding="utf-8")
    assert fs.gerber_file_function(plain) is None      # attributes after the apertures: no header


def test_fixed_gbrjob_text_is_pure():
    text = JOBS["k9_6l"].read_text(encoding="utf-8")
    out, changes = fs.fixed_gbrjob_text(text, {"s6L-In1_Cu.g1": "Copper,L2,Inr"})
    assert len(changes) == 1 and out != text and text == JOBS["k9_6l"].read_text(encoding="utf-8")
    assert fs.fixed_gbrjob_text(text, {}) == (text, [])
    with pytest.raises(ValueError, match="FilesAttributes"):
        fs.fixed_gbrjob_text('{"MaterialStackup": []}', {})


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_fixed_gbrjob_text_reserialises_odd_entries(newline):
    """An entry without FileFunction: the entries cannot be matched in place, so the
    file is re-serialised KiCad-style with its own line ends."""
    job = {"FilesAttributes": [{"Path": "a-In1_Cu.g1", "FileFunction": "Copper,L5,Inr"},
                               {"Path": "notes.txt"}], "MaterialStackup": []}
    text = json.dumps(job, indent=2).replace("\n", newline) + newline
    out, changes = fs.fixed_gbrjob_text(text, {"a-In1_Cu.g1": "Copper,L2,Inr"})
    assert changes == [{"path": "a-In1_Cu.g1", "from": "Copper,L5,Inr", "to": "Copper,L2,Inr"}]
    assert json.loads(out)["FilesAttributes"] == [
        {"Path": "a-In1_Cu.g1", "FileFunction": "Copper,L2,Inr"}, {"Path": "notes.txt"}]
    assert out.endswith(newline) and out.count(newline) == out.count("\n")


# ---------------------------------------------------------------------------
# expected_thickness_rows
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name, rows", [("d722_4l_hybrid", 10), ("d722_6l_2116", 16),
                                        ("s6l_hybrid", 15), ("s6l_k9", 14),
                                        ("d722_2l_mixed", 5), ("single_sided", 5)])
def test_expected_thickness_rows(name, rows):
    rs = _rs(_design(name))
    got = fs.expected_thickness_rows(rs)
    assert len(got) == rows == len(rs.copper) + sum(len(d.plies) for d in rs.dielectrics) + 2
    assert [r["type"] for r in (got[0], got[1], got[-2], got[-1])] == [
        "SolderMask", "Copper", "Copper", "SolderMask"]
    assert sum(r["thickness"] for r in got) == pytest.approx(rs.board_thickness_computed, abs=1e-6)
    assert fs.expected_thickness_rows(rs.to_dict()) == got
    assert fs.expected_thickness_rows({"schema": pr.SCHEMA, "stackup": rs.to_dict()}) == got


def test_expected_rows_name_the_plies_like_kicad():
    got = fs.expected_thickness_rows(_rs(_design("s6l_hybrid")))
    names = [r["name"] for r in got if r["type"] == "Dielectric"]
    assert names[:3] == ["F.Cu/In1.Cu", "In1.Cu/In2.Cu (1/2)", "In1.Cu/In2.Cu (2/2)"]
    first = next(r for r in got if r["type"] == "Dielectric")
    assert (first["material"], first["dk"], first["df"]) == ("RO4350B", 3.66, 0.0037)


def test_expected_rows_refuse_the_legacy_shorthand_and_errors():
    with pytest.raises(ValueError, match="legacy"):
        fs.expected_thickness_rows(_rs(_design("legacy")))
    with pytest.raises(ValueError, match="no stackup"):
        fs.expected_thickness_rows({"schema": pr.SCHEMA, "stackup": None})
    with pytest.raises(TypeError):
        fs.expected_thickness_rows(42)


# ---------------------------------------------------------------------------
# check_gbrjob
# ---------------------------------------------------------------------------
def test_check_passes_on_the_1005_fixture_without_a_fix():
    result = fs.check_gbrjob(JOBS["k10"], _rs(_design("s6l_hybrid")))
    assert result["ok"] and result["problems"] == [], result
    assert (result["rows"], result["expected_rows"]) == (15, 15)


@pytest.mark.parametrize("src, design, rows", [(K9_6L, "s6l_k9", 14),
                                               (K9_4L, "d722_4l_hybrid", 10)])
def test_check_passes_on_fixed_904_job_files(tmp_path, src, design, rows):
    job = _fixed(src, tmp_path, design)
    result = fs.check_gbrjob(job, _artifact(_design(design), tmp_path))
    assert result["ok"] and result["problems"] == [], result
    assert result["rows"] == rows


def test_check_flags_the_unfixed_904_numbering():
    result = fs.check_gbrjob(JOBS["k9_6l"], _rs(_design("s6l_k9")))
    assert not result["ok"]
    assert len(result["problems"]) == 4
    assert all("fix_gbrjob_file_attributes" in p for p in result["problems"])
    assert "s6L-In1_Cu.g1" in result["problems"][0] and "L5" in result["problems"][0]


def _mutated(path: Path, change) -> dict:
    job = _job(path)
    change(job)
    return job


def _drop_ply(job: dict) -> None:
    job["MaterialStackup"] = [r for r in job["MaterialStackup"]
                              if r.get("Name") != "In1.Cu/In2.Cu (2/2)"]


def _row(job: dict, name: str) -> dict:
    return next(r for r in job["MaterialStackup"] if r.get("Name") == name)


MUTATIONS = {
    "rows": (_drop_ply, "thickness rows"),
    "thickness": (lambda j: _row(j, "In2.Cu").update(Thickness=0.0172), "In2.Cu"),
    "material": (lambda j: _row(j, "F.Cu/In1.Cu").update(Material="FR4"), "RO4350B"),
    "dk": (lambda j: _row(j, "F.Cu/In1.Cu").update(DielectricConstant="3.48"), "3.66"),
    "df": (lambda j: _row(j, "F.Cu/In1.Cu").update(LossTangent="0.02"), "loss tangent"),
    "mask": (lambda j: _row(j, "Top Solder Mask").update(Thickness=0.02), "Top Solder Mask"),
    "impedance": (lambda j: j["GeneralSpecs"].pop("ImpedanceControlled"), "ImpedanceControlled"),
    "board": (lambda j: j["GeneralSpecs"].update(BoardThickness=1.6), "BoardThickness"),
    "count": (lambda j: j["GeneralSpecs"].update(LayerNumber=4), "LayerNumber"),
    "finish": (lambda j: j["GeneralSpecs"].update(Finish="OSP"), "Finish"),
    "type": (lambda j: _row(j, "In2.Cu").update(Type="Dielectric"), "Copper"),
}


@pytest.mark.parametrize("what", MUTATIONS)
def test_check_flags_each_difference(what):
    change, word = MUTATIONS[what]
    result = fs.check_gbrjob(_mutated(JOBS["k10"], change), _rs(_design("s6l_hybrid")))
    assert not result["ok"] and result["problems"], what
    assert any(word in p for p in result["problems"]), result["problems"]


def test_check_tolerates_rounding_within_a_micron():
    def nudge(job: dict) -> None:
        _row(job, "In2.Cu").update(Thickness=0.0161)
        job["GeneralSpecs"]["BoardThickness"] = 1.6293
        _row(job, "F.Cu/In1.Cu").update(DielectricConstant="3.66001")
    result = fs.check_gbrjob(_mutated(JOBS["k10"], nudge), _rs(_design("s6l_hybrid")))
    assert result["ok"], result


def _without_dk(job: dict) -> None:
    for r in job["MaterialStackup"]:
        r.pop("DielectricConstant", None)
        r.pop("LossTangent", None)


def test_dk_is_required_only_on_impedance_builds():
    """KiCad writes Dk / loss tangent only with (dielectric_constraints yes)."""
    plain = _design("s6l_hybrid")
    plain = {**plain, "stackup": {k: v for k, v in plain["stackup"].items()
                                  if k != "impedance_control"}}
    job = _mutated(JOBS["k10"], _without_dk)
    job["GeneralSpecs"].pop("ImpedanceControlled")
    assert fs.check_gbrjob(job, _rs(plain))["ok"]
    strict = fs.check_gbrjob(_mutated(JOBS["k10"], _without_dk), _rs(_design("s6l_hybrid")))
    assert not strict["ok"] and any("Dk None" in p for p in strict["problems"])


def test_import_without_dk_takes_the_library_material():
    def change(job: dict) -> None:
        _without_dk(job)
        _row(job, "F.Cu/In1.Cu").update(Material="NP-155F 2116")
        _row(job, "In2.Cu/In3.Cu").update(Material="Mystery")
    layers = fs.netlist_stackup_from_gbrjob(_mutated(JOBS["k10"], change))["layers"]
    assert layers[1] == {"type": "dielectric", "kind": "prepreg", "thickness": 0.254,
                         "material": "nanya.np155f", "label": "NP-155F 2116",
                         "construction": "2116"}
    assert layers[5] == {"type": "dielectric", "kind": "core", "thickness": 0.45,
                         "material": "Mystery", "dk": 4.5, "df": 0.02}
    assert layers[9] == {"type": "dielectric", "kind": "core", "thickness": 0.254,
                         "material": "generic.fr4", "label": "FR4 core"}   # library FR-4 4.5
    rs = _rs({**_design("s6l_hybrid"), "stackup": fs.netlist_stackup_from_gbrjob(
        _mutated(JOBS["k10"], change))})
    assert rs.dielectrics[0].plies[0].material_id == "nanya.np155f"


def test_check_without_thickness_rows_says_why():
    def strip(job: dict) -> None:
        for r in job["MaterialStackup"]:
            r.pop("Thickness", None)
    result = fs.check_gbrjob(_mutated(JOBS["k10"], strip), _rs(_design("s6l_hybrid")))
    assert not result["ok"] and result["rows"] == 0
    assert any("technical layers" in p for p in result["problems"])


def test_check_skips_the_rows_of_the_legacy_shorthand():
    """No KiCad stackup block is written for {h, er, t}: KiCad's own default stackup
    fills the job file, so only the copper count, numbering and flags are checked."""
    job = _job(JOBS["k9_4l"])
    job["FilesAttributes"] = [{**e, "FileFunction": e["FileFunction"].replace("L5", "L2")
                               .replace("L7", "L3")} for e in job["FilesAttributes"]]
    job["GeneralSpecs"].pop("ImpedanceControlled")
    result = fs.check_gbrjob(job, _rs(_design("legacy")))
    assert result["ok"] and result["expected_rows"] is None, result
    assert any("legacy" in n for n in result["notes"])
    unfixed = fs.check_gbrjob({**job, "FilesAttributes": _job(JOBS["k9_4l"])["FilesAttributes"]},
                              _rs(_design("legacy")))
    assert not unfixed["ok"] and len(unfixed["problems"]) == 2


def test_check_accepts_text_and_refuses_garbage(tmp_path):
    text = JOBS["k10"].read_text(encoding="utf-8")
    assert fs.check_gbrjob(text, _rs(_design("s6l_hybrid")))["ok"]
    with pytest.raises(ValueError, match="gbrjob"):
        fs.check_gbrjob("{not json", _rs(_design("s6l_hybrid")))
    with pytest.raises(OSError):
        fs.check_gbrjob(tmp_path / "missing-job.gbrjob", _rs(_design("s6l_hybrid")))


# ---------------------------------------------------------------------------
# ipc2581_needed
# ---------------------------------------------------------------------------
def _two_layer(material: str, dk: float | None = None, impedance: bool = False) -> dict:
    core = {"type": "dielectric", "kind": "core", "material": material, "thickness": 1.51}
    st = {"layers": [{"type": "copper", "role": "signal"}, {**core, **({"dk": dk} if dk else {})},
                     {"type": "copper", "role": "mixed", "net": "GND"}]}
    if impedance:
        st["impedance_control"] = {"required": True}
    return {"name": "x", "stackup": st}


@pytest.mark.parametrize("design, needed, word", [
    (_two_layer("generic.fr4"), False, None),
    (_two_layer("FR4 core", dk=4.5), False, None),
    (_two_layer("generic.fr4", impedance=True), True, "impedance"),
    (_two_layer("rogers.ro4003c"), True, "RO4003C"),
    (_two_layer("RO4350B", dk=3.66), True, "RO4350B"),
    (_two_layer("Mystery laminate", dk=3.0), True, "Mystery laminate"),
    ({"name": "x", "copper_layers": 4}, True, "4 copper layers"),
])
def test_ipc2581_rule(design, needed, word):
    rs = _rs(design)
    assert fs.ipc2581_needed(rs) is needed
    reasons = fs.ipc2581_reasons(rs)
    assert bool(reasons) is needed and (word is None or any(word in r for r in reasons)), reasons


# ---------------------------------------------------------------------------
# stackup_csv
# ---------------------------------------------------------------------------
def test_stackup_csv_lists_every_layer_with_its_dk_basis():
    rs = _rs(_design("d722_4l_hybrid"))
    text = fs.stackup_csv(rs)
    assert text == fs.stackup_csv(rs.to_dict()) and "\r" not in text
    rows = list(csv.DictReader(io.StringIO(text)))
    assert list(rows[0]) == list(fs.CSV_COLUMNS)
    assert [r["type"] for r in rows] == (["solder_mask", "copper", "dielectric", "copper",
                                          "dielectric", "dielectric", "copper", "dielectric",
                                          "copper", "solder_mask", "total"])
    ro = rows[2]       # design Dk 3.66 is characterised 8-40 GHz: the nearest point (ST-W6)
    assert (ro["material"], ro["material_id"], ro["dk"], ro["dk_basis"], ro["dk_freq_ghz"]) == (
        "RO4350B", "rogers.ro4350b", "3.66", "design", "8")
    assert (ro["kind"], ro["locked"], ro["thickness_mm"]) == ("core", "yes", "0.254")
    assert (rows[3]["role"], rows[3]["net"], rows[3]["oz"]) == ("plane", "GND", "1")
    assert rows[0]["material"] == "LPI" and rows[0]["source"] == "generic"
    assert rows[-1]["thickness_mm"] == "1.3685"
    assert sum(float(r["thickness_mm"]) for r in rows[:-1]) == pytest.approx(1.3685, abs=1e-6)


def test_stackup_csv_without_mask():
    rows = list(csv.DictReader(io.StringIO(fs.stackup_csv(
        _rs({**_design("d722_2l_mixed"), "stackup": {**_design("d722_2l_mixed")["stackup"],
                                                      "mask": None}})))))
    assert (rows[0]["source"], rows[0]["thickness_mm"], rows[0]["material"]) == ("none", "0", "")


# ---------------------------------------------------------------------------
# fab_notes_md
# ---------------------------------------------------------------------------
def _cp2102n_6l(tmp_path: Path) -> tuple[dict, dict]:
    design = json.loads((FIXTURES.parent.parent / "examples" / "cp2102n_uart" / "netlist.json")
                        .read_text(encoding="utf-8"))
    design = {**design, "stackup": _design("d722_6l_2116")["stackup"],
              "net_specs": {"USB_DP": {"diff_pair": "USB_DM", "diff": 90, "gap": 0.15},
                            "VBUS": {"impedance": 50}}}
    return design, _artifact(design, tmp_path)


def test_notes_for_a_jlc_impedance_build(tmp_path):
    design, artifact = _cp2102n_6l(tmp_path)
    md = fs.fab_notes_md(artifact, design=design, stem="cp2102n_uart",
                         human_steps=["VNA-tune the pi network"])
    for text in ("jlcpcb:JLC06161H-2116", "6L: two inner routing layers for the MCU fan-out",
                 "1.6554", "ordered 1.6 mm", "select FR-4 TG155 for JLC impedance builds",
                 "unverified", "| USB_DP / USB_DM | F.Cu | microstrip | 90 ohm Zdiff",
                 "| VBUS | F.Cu | microstrip | 50 ohm Z0", "+/-10 %", "In1.Cu GND",
                 "VNA-tune the pi network", "cp2102n_uart_stackup.csv",
                 "cp2102n_uart_ipc2581.xml", "the fab's solder mask"):
        assert text in md, text
    assert "UNVERIFIED" in md and "USB_DP / USB_DM on In2.Cu" in md      # class C fallbacks
    assert "single-sided" not in md and "Hybrid" not in md
    assert md == fs.fab_notes_md(artifact, design=design, stem="cp2102n_uart",
                                 human_steps=["VNA-tune the pi network"])
    assert not re.search(r"\d{4}-\d{2}-\d{2}T", md)                     # no timestamps


def test_notes_hybrid_mask_and_single_sided_callouts(tmp_path):
    hybrid = fs.fab_notes_md(_rs(_design("d722_4l_hybrid")))
    assert "Hybrid" in hybrid and "fab to confirm with Rogers" in hybrid
    assert "generic LPI" in hybrid and "TG155" not in hybrid
    assert "Rogers under the RF feeds only" in hybrid
    assert "Hybrid" in fs.fab_notes_md(_rs(_design("s6l_hybrid")))      # label-only FR-4 plies
    nomask = _design("d722_2l_mixed")
    nomask = {**nomask, "stackup": {**nomask["stackup"], "mask": None}}
    md = fs.fab_notes_md(_rs(nomask))
    assert "open the solder mask over the listed impedance lines" in md
    single = fs.fab_notes_md(_artifact(_design("single_sided"), tmp_path))
    assert "single-sided: B.Cu holds only through-hole pad lands" in single
    assert "fab wording unverified" in single
    assert "No controlled-impedance nets" in single
    assert "without the physical-design artifact" in fs.fab_notes_md(_rs(_design("single_sided")))
    policy = fs.fab_notes_md(_rs(_design("policy_origin")))
    assert "napkin" in policy and "jlcpcb:JLC04161H-7628" in policy


def test_notes_list_a_line_without_geometry():
    rs = _rs(_design("d722_4l_hybrid"))
    artifact = {"schema": pr.SCHEMA, "stackup": rs.to_dict(),
                "lines": {"RF": {"kind": "single", "line": "cpwg", "z_target": 50,
                                 "status": "error", "layers": {}}}}
    md = fs.fab_notes_md(artifact)
    assert "| RF | - | cpwg | 50 ohm | - | - | +/-10 % | - | - | no geometry (error) |" in md


def test_notes_for_the_legacy_shorthand(tmp_path):
    md = fs.fab_notes_md(_artifact(_design("legacy"), tmp_path))
    assert "legacy" in md and "| RF_IN | F.Cu | microstrip | 50 ohm Z0" in md
    assert "open the solder mask" not in md


def test_write_outputs_is_byte_stable(tmp_path):
    design, artifact = _cp2102n_6l(tmp_path)
    before = copy.deepcopy(artifact)
    first = fs.write_outputs(tmp_path / "out", "board", artifact, design=design)
    assert sorted(Path(p).name for p in first.values()) == ["board_fab_notes.md",
                                                            "board_stackup.csv"]
    data = {k: Path(p).read_bytes() for k, p in first.items()}
    assert fs.write_outputs(tmp_path / "out", "board", artifact, design=design) == first
    assert {k: Path(p).read_bytes() for k, p in first.items()} == data
    assert artifact == before


# ---------------------------------------------------------------------------
# netlist_stackup_from_gbrjob
# ---------------------------------------------------------------------------
def _plies(rs: sm.ResolvedStackup) -> list[list[tuple]]:
    """Each ply as KiCad writes it to the job file (Er '%g', loss tangent FormatDouble2Str)."""
    return [[(p.thickness, sk.kicad_er(p.er), sk.kicad_double(p.df), sk.ply_label(p))
             for p in d.plies] for d in rs.dielectrics]


def test_import_reproduces_the_1005_fixture():
    stackup = fs.netlist_stackup_from_gbrjob(JOBS["k10"])
    assert (stackup["origin"], stackup["rationale"]) == (
        "designer", "imported from Gerber job file k10-job.gbrjob")
    assert stackup["finish"] == "ENIG" and stackup["impedance_control"] == {"required": True}
    assert stackup["board_thickness"] == 1.6284
    assert stackup["mask"] == {"over_substrate": 0.01, "material": "LPI", "dk": 3.8, "df": 0.025,
                               "color": "Green"}
    design = {**_design("s6l_hybrid"), "stackup": stackup}
    got, want = _rs(design), _rs(_design("s6l_hybrid"))
    assert _plies(got) == _plies(want)
    assert [d.kind for d in got.dielectrics] == [d.kind for d in want.dielectrics]
    assert [(c.name, c.thickness) for c in got.copper] == [(c.name, c.thickness)
                                                           for c in want.copper]
    assert got.board_thickness_computed == want.board_thickness_computed == 1.6284
    assert fs.check_gbrjob(JOBS["k10"], got)["ok"]
    assert netlist_schema.validate(design) == []
    _, issues = sm.resolve(design)
    assert not [i for i in issues if i.id.startswith("ST-E")], issues


@pytest.mark.parametrize("key, design, thickness", [("k9_6l", "s6l_k9", 1.4908),
                                                    ("k9_4l", "d722_4l_hybrid", 1.3685)])
def test_import_reads_the_904_job_files(key, design, thickness):
    stackup = fs.netlist_stackup_from_gbrjob(JOBS[key])
    got, want = _rs({**_design(design), "stackup": stackup}), _rs(_design(design))
    assert _plies(got) == _plies(want)
    assert got.board_thickness_computed == pytest.approx(thickness, abs=1e-4)
    assert len(fs.expected_thickness_rows(got)) == sum("Thickness" in r for r in
                                                       _job(JOBS[key])["MaterialStackup"])


def test_import_without_mask_and_with_plane_types():
    def change(job: dict) -> None:
        for r in job["MaterialStackup"]:
            if r["Type"] == "SolderMask":
                r["Thickness"] = 0
        job["FilesAttributes"][1]["FileFunction"] = "Copper,L2,Inr,Plane"
        job["GeneralSpecs"]["Finish"] = "HAL lead-free"
        job["GeneralSpecs"].pop("ImpedanceControlled")
    job = _mutated(JOBS["k10"], change)
    stackup = fs.netlist_stackup_from_gbrjob(job)
    assert stackup["mask"] is None and "impedance_control" not in stackup
    assert stackup["finish"] == "LF-HASL"
    assert stackup["layers"][2] == {"type": "copper", "name": "In1.Cu", "thickness": 0.035,
                                    "role": "plane"}
    assert "role" not in stackup["layers"][0]


@pytest.mark.parametrize("change, word", [
    (lambda j: j.pop("MaterialStackup"), "MaterialStackup"),
    (lambda j: j["MaterialStackup"].pop(3), "alternate"),
    (lambda j: j["GeneralSpecs"].update(LayerNumber=4), "LayerNumber"),
    (lambda j: [r.pop("Thickness", None) for r in j["MaterialStackup"]], "thickness"),
])
def test_import_refuses_broken_job_files(change, word):
    with pytest.raises(ValueError, match=word):
        fs.netlist_stackup_from_gbrjob(_mutated(JOBS["k10"], change))


def test_inputs_are_never_mutated():
    job = _job(JOBS["k10"])
    rs = _rs(_design("s6l_hybrid"))
    snapshot, data = copy.deepcopy(job), rs.to_dict()
    fs.check_gbrjob(job, data)
    fs.netlist_stackup_from_gbrjob(job)
    fs.fab_notes_md(data)
    fs.stackup_csv(data)
    assert job == snapshot and data == rs.to_dict()


# ---------------------------------------------------------------------------
# live tier: KiCad 9.0.4 writes the job file, fab_stackup fixes and checks it
# ---------------------------------------------------------------------------
LIVE = {"6l_2116": "d722_6l_2116", "2l_nomask": "d722_2l_mixed"}


def _cli(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL, timeout=300)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0 and not ERROR_LINE.search(out), (args, out[-2000:])


def _live_design(name: str) -> dict:
    design = _design(LIVE[name])
    if name == "2l_nomask":
        design = {**design, "stackup": {**design["stackup"], "mask": None}}
    return design


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> dict:
    kpy = require_kicad_python()
    work = tmp_path_factory.mktemp("fab_stackup_live")
    specs = [{"name": n, "design": _live_design(n)} for n in LIVE]
    (work / "boards.json").write_text(json.dumps(specs), encoding="utf-8")
    script = (FIXTURES / "stackup_kicad" / "build_board.py").read_text(encoding="utf-8")
    fp_dir = kicad_footprints(kpy, "Connector_PinHeader_2.54mm.pretty")
    res = run_child(script, [work / "boards.json", work, fp_dir, TOOLS], work, timeout=900)
    assert not {n: b["error"] for n, b in res["boards"].items() if "error" in b}
    cli = toolchain.board_cli(res["boards"]["6l_2116"]["pcb"])
    if not cli:
        pytest.skip("no kicad-cli for the built boards")
    out = {}
    for name, board in res["boards"].items():
        pcb = Path(board["pcb"])
        gerbers = pcb.parent / f"{pcb.stem}_gerbers"
        gerbers.mkdir(exist_ok=True)
        _cli([cli, "pcb", "export", "gerbers", "-o", str(gerbers) + os.sep, str(pcb)])
        job = gerbers / f"{pcb.stem}-job.gbrjob"
        out[name] = {"pcb": pcb, "job": job, "raw": _job(job),
                     "fix": fs.fix_gbrjob_file_attributes(job, gerbers)}
    return {"cli": cli, "boards": out}


def test_live_6l_job_file_is_fixed_and_checks(live, tmp_path):
    board = live["boards"]["6l_2116"]
    assert board["fix"]["problems"] == []
    assert _copper_functions(_job(board["job"]))[1:5] == [f"Copper,L{k},Inr" for k in (2, 3, 4, 5)]
    result = fs.check_gbrjob(board["job"], _artifact(_live_design("6l_2116"), tmp_path))
    assert result["ok"] and result["rows"] == 16, result
    version = board["raw"]["Header"]["GenerationSoftware"]["Version"]
    assert bool(board["fix"]["changed"]) is version.startswith("9.0.4"), board["fix"]


def test_live_ipc2581_and_text_outputs(live, tmp_path):
    rs6, rs2 = _rs(_live_design("6l_2116")), _rs(_live_design("2l_nomask"))
    assert fs.ipc2581_needed(rs6) and not fs.ipc2581_needed(rs2)
    pcb = live["boards"]["6l_2116"]["pcb"]
    xml = pcb.with_name(pcb.stem + fs.IPC2581_SUFFIX)
    _cli([live["cli"], "pcb", "export", "ipc2581", "-o", str(xml), str(pcb)])
    assert "NP-155F" in xml.read_text(encoding="utf-8", errors="replace")
    paths = fs.write_outputs(tmp_path, pcb.stem, rs6)
    assert Path(paths["csv"]).read_text(encoding="utf-8").count("\n") == 16 + 2


def test_live_board_without_mask(live, tmp_path):
    board = live["boards"]["2l_nomask"]
    assert board["fix"] == {"path": str(board["job"]), "changed": [], "problems": [],
                            "written": False}
    rs = _rs(_live_design("2l_nomask"))
    result = fs.check_gbrjob(board["job"], rs)
    assert result["ok"] and result["rows"] == 5, result
    stackup = fs.netlist_stackup_from_gbrjob(board["job"])
    assert stackup["mask"] is None
    assert _plies(_rs({**_live_design("2l_nomask"), "stackup": stackup})) == _plies(rs)
