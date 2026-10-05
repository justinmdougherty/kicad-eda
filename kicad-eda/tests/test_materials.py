"""Materials library (server/data/materials/*.json, server/tools/materials.py), ADR D7.3.

Unit tier:
  * the library loads, rejects duplicate ids and malformed entries, and its
    data_version is the sha256 of the sorted data files (changes with a byte);
  * every Dk/Df entry carries a basis, a frequency and a source URL (an
    untraced value is labelled typical and says so in a note);
  * resolve_dk / resolve_df follow the D7.3 order (explicit > fab > design >
    process > typical, most specific `when` first, log-frequency
    interpolation, nearest value out of band) with ST-W5 (thin core),
    ST-W6 (out of band) and ST-W8 (conflicting sources) warnings;
  * the D7.1 values: RO4350B process 3.48 / 4 mil 3.33 / design 3.66,
    NP-155F JLC fab entries (7628 4.4, core 4.41/4.43, alt core 4.6),
    S1000-2M 2313 4.31.

Live tier (skipped without KiCad 9): materials.py imports and resolves under
KiCad 9's bundled Python, pulling in nothing outside the stdlib.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import materials as mat

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "server" / "tools"
DATA = ROOT / "server" / "data" / "materials"
LIVE_TIMEOUT_S = 60

SEED_IDS = {
    "rogers.ro4003c", "rogers.ro4350b", "rogers.ro4835", "rogers.ro3003", "rogers.ro3010",
    "rogers.rt5880", "rogers.ro4450f", "isola.370hr", "isola.fr408hr", "panasonic.megtron6",
    "shengyi.s1000-2m", "nanya.np155f", "agc.tly5", "taconic.rf35", "arlon.25n", "arlon.25fr",
    "zyf.zyf255da", "zyf.zyf265d", "zyf.zyf300ca-c", "zyf.zyf300ca-p", "generic.fr4", "lpi",
}
JLC_IMPEDANCE = "https://jlcpcb.com/impedance"


@pytest.fixture(scope="module")
def lib() -> mat.MaterialLibrary:
    return mat.load_library()


def ids_of(rv: mat.ResolvedValue) -> list[str]:
    return [w.id for w in rv.warnings]


# ---------------------------------------------------------------------------
# 1. loading, data_version, validation
# ---------------------------------------------------------------------------
def test_library_holds_the_seed_set(lib):
    assert SEED_IDS <= set(lib.ids())
    assert list(lib.ids()) == sorted(lib.ids())
    assert "rogers.ro4350b" in lib and "nope.nothing" not in lib
    assert lib.get("nope.nothing") is None


def test_data_version_is_sha256_of_the_sorted_files(lib):
    h = hashlib.sha256()
    for path in sorted(DATA.glob("*.json"), key=lambda p: p.name):
        h.update(path.name.encode("utf-8") + b"\0")
        h.update(path.read_bytes() + b"\0")
    assert lib.data_version == h.hexdigest()
    assert lib.data_version == mat.load_library().data_version
    assert [Path(f).name for f in lib.files] == sorted(p.name for p in DATA.glob("*.json"))


def test_data_version_changes_when_a_file_changes(tmp_path):
    root = tmp_path / "m"
    shutil.copytree(DATA, root)
    before = mat.load_library(root).data_version
    path = root / "generic.json"
    path.write_bytes(path.read_bytes() + b"\n")
    assert mat.load_library(root).data_version != before


def test_duplicate_material_id_is_rejected(tmp_path):
    root = tmp_path / "m"
    shutil.copytree(DATA, root)
    shutil.copy(root / "rogers.json", root / "rogers_copy.json")
    with pytest.raises(ValueError, match=r"duplicate material id 'rogers\.ro4003c'.*rogers"):
        mat.load_library(root)


@pytest.mark.parametrize("mutation, needle", [
    (lambda e: e.update(basis="guess"), "basis"),
    (lambda e: e.pop("freq_hz"), "freq_hz"),
    (lambda e: e.update(when={"colour": "green"}), "when"),
    (lambda e: e.update(value="4.5"), "value"),
])
def test_malformed_entries_are_rejected(tmp_path, mutation, needle):
    data = {"schema": mat.SCHEMA, "materials": {"x.y": {
        "vendor": "x", "product": "y", "category": "fr4", "forms": ["core"],
        "dk": [{"value": 4.5, "basis": "typical", "freq_hz": 1e9, "source": "https://x.test/"}],
        "df": [], "sources": [{"title": "t", "url": "https://x.test/"}]}}}
    mutation(data["materials"]["x.y"]["dk"][0])
    (tmp_path / "x.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match=needle):
        mat.load_library(tmp_path)


def test_wrong_schema_is_rejected(tmp_path):
    (tmp_path / "x.json").write_text(json.dumps({"schema": "other", "materials": {}}),
                                     encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        mat.load_library(tmp_path)


def test_get_returns_a_copy(lib):
    m = lib.get("rogers.ro4350b")
    m["dk"].clear()
    assert lib.get("rogers.ro4350b")["dk"], "the library was mutated through get()"


# ---------------------------------------------------------------------------
# 2. every value is sourced
# ---------------------------------------------------------------------------
def _entries(lib):
    for mid in lib.ids():
        m = lib.get(mid)
        for prop in ("dk", "df"):
            for e in m[prop]:
                yield mid, prop, e


def test_every_value_has_basis_frequency_and_a_source(lib):
    n = 0
    for mid, prop, e in _entries(lib):
        n += 1
        where = f"{mid}.{prop} {e}"
        assert isinstance(e["value"], (int, float)) and e["value"] > 0, where
        assert e["basis"] in mat.BASES, where
        f = e["freq_hz"]
        if isinstance(f, list):
            assert len(f) == 2 and 0 < f[0] < f[1], where
        else:
            assert isinstance(f, (int, float)) and f > 0, where
        if e["basis"] == "fab":
            assert e.get("fab"), where
        if e.get("untraced"):
            assert e["basis"] == "typical" and "untraced" in e.get("note", ""), where
        else:
            assert str(e.get("source", "")).startswith("https://"), where
            urls = {s["url"] for s in lib.get(mid)["sources"]}
            assert e["source"] in urls, f"{where}: source not in the material's sources"
    assert n > 60


def test_every_material_has_a_dk_and_a_category(lib):
    for mid in lib.ids():
        m = lib.get(mid)
        assert m["dk"], mid
        assert m["category"] in mat.CATEGORIES, mid
        assert m["sources"] and all(s["url"].startswith("https://") for s in m["sources"]), mid


def test_jlc_snapshot_fab_entries(lib):
    """ADR D7.3 seed: NP-155F JLC fab entries 7628 4.4, 3313 4.1, 2116 4.16, 1080 3.91,
    core 4.41/4.43 (snapshot) and the alt core 4.6 (jlcpcb.com/impedance)."""
    fab = {(e.get("when", {}).get("construction"), e["value"], bool(e.get("alt")))
           for e in lib.get("nanya.np155f")["dk"] if e["basis"] == "fab"}
    for c, v in [("7628", 4.4), ("3313", 4.1), ("2116", 4.16), ("1080", 3.91),
                 ("core", 4.41), ("core", 4.43)]:
        assert (c, v, False) in fab
    assert ("core", 4.6, True) in fab
    s1000 = {(e.get("when", {}).get("construction"), e["value"])
             for e in lib.get("shengyi.s1000-2m")["dk"] if e["basis"] == "fab"}
    assert ("2313", 4.31) in s1000


def test_ro4350b_design_entry_carries_the_thin_core_interpretation(lib):
    design = [e for e in lib.get("rogers.ro4350b")["dk"] if e["basis"] == "design"]
    assert len(design) == 1
    d = design[0]
    assert d["value"] == 3.66 and d["freq_hz"] == [8e9, 4e10]
    assert d["valid_thickness_mm"] == [0.508, 1.524]
    assert d.get("interpretation") is True and "0.1" in d["note"]


# ---------------------------------------------------------------------------
# 3. resolution order (D7.3) and warnings
# ---------------------------------------------------------------------------
def test_ro4350b_process_design_and_thin_core(lib):
    proc = mat.resolve_dk(lib, "rogers.ro4350b", freq_hz=1e10, policy="process",
                          thickness_mm=0.254)
    assert (proc.value, proc.basis, proc.warnings) == (3.48, "process", ())
    thin = mat.resolve_dk(lib, "rogers.ro4350b", freq_hz=1e10, policy="process",
                          thickness_mm=0.1016)
    assert (thin.value, thin.basis) == (3.33, "process")
    design = mat.resolve_dk(lib, "rogers.ro4350b", freq_hz=2e10, policy="design",
                            thickness_mm=0.762)
    assert (design.value, design.basis, design.warnings) == (3.66, "design", ())
    assert design.source.startswith("https://www.rogerscorp.com/")


def test_ro4350b_design_dk_at_lora_frequency_is_one_st_w6(lib):
    rv = mat.resolve_dk(lib, "rogers.ro4350b", freq_hz=9.15e8, policy="design",
                        thickness_mm=0.508)
    assert (rv.value, rv.basis) == (3.66, "design")
    assert ids_of(rv) == ["ST-W6"]
    assert rv.warnings[0].severity == "info"
    assert rv.freq_hz == 8e9                    # the nearest characterised frequency
    assert "9.15e+08" in rv.warnings[0].message or "915" in rv.warnings[0].message


def test_ro4350b_design_dk_on_a_thin_core_is_st_w5(lib):
    rv = mat.resolve_dk(lib, "rogers.ro4350b", freq_hz=1e10, policy="design",
                        thickness_mm=0.254)
    assert rv.value == 3.66 and ids_of(rv) == ["ST-W5"]
    assert rv.warnings[0].severity == "warning" and "3.33" in rv.warnings[0].message
    both = mat.resolve_dk(lib, "rogers.ro4350b", freq_hz=1.575e9, policy="design",
                          thickness_mm=0.254)
    assert sorted(ids_of(both)) == ["ST-W5", "ST-W6"]


def test_auto_policy_is_fab_only_when_the_fab_controls_impedance(lib):
    kw = {"freq_hz": 9.15e8, "fab": "jlcpcb", "construction": "7628", "thickness_mm": 0.2104}
    fab = mat.resolve_dk(lib, "nanya.np155f", policy="auto", fab_controls_impedance=True, **kw)
    assert (fab.value, fab.basis, fab.source) == (4.4, "fab", JLC_IMPEDANCE)
    assert fab.warnings == ()
    nofab = mat.resolve_dk(lib, "nanya.np155f", policy="auto", fab_controls_impedance=False,
                           **kw)
    assert (nofab.value, nofab.basis) == (4.3, "typical")
    other = mat.resolve_dk(lib, "nanya.np155f", policy="fab", **{**kw, "fab": "pcbway"})
    assert other.basis == "typical"             # JLC's numbers never apply to another fab


@pytest.mark.parametrize("construction, thickness, value", [
    ("7628", 0.2104, 4.4), ("3313", 0.0994, 4.1), ("2116", 0.127, 4.16), ("1080", 0.0764, 3.91),
    ("core", 0.55, 4.41), ("core", 0.3, 4.41), ("core", 1.065, 4.43), ("core", 1.265, 4.43),
])
def test_np155f_jlc_fab_values(lib, construction, thickness, value):
    rv = mat.resolve_dk(lib, "nanya.np155f", freq_hz=1e9, policy="fab", fab="jlcpcb",
                        construction=construction, thickness_mm=thickness)
    assert (rv.value, rv.basis) == (value, "fab")


def test_jlc_core_conflict_with_the_impedance_page_is_st_w8(lib):
    rv = mat.resolve_dk(lib, "nanya.np155f", freq_hz=1e9, policy="fab", fab="jlcpcb",
                        construction="core", thickness_mm=1.065)
    assert rv.value == 4.43 and ids_of(rv) == ["ST-W8"]
    assert "4.6" in rv.warnings[0].message and JLC_IMPEDANCE in rv.warnings[0].message
    prepreg = mat.resolve_dk(lib, "nanya.np155f", freq_hz=1e9, policy="fab", fab="jlcpcb",
                             construction="7628", thickness_mm=0.2104)
    assert "ST-W8" not in ids_of(prepreg)


def test_s1000_2m_2313_at_jlc(lib):
    rv = mat.resolve_dk(lib, "shengyi.s1000-2m", freq_hz=1e9, policy="fab", fab="jlcpcb",
                        construction="2313", thickness_mm=0.0964)
    assert (rv.value, rv.basis) == (4.31, "fab")


def test_typical_values_interpolate_in_log_frequency(lib):
    at_1g = mat.resolve_dk(lib, "isola.370hr", freq_hz=1e9, policy="design")
    assert (at_1g.value, at_1g.basis, at_1g.freq_hz) == (4.17, "typical", 1e9)
    gnss = mat.resolve_dk(lib, "isola.370hr", freq_hz=1.575e9, policy="design")
    t = math.log(1.575) / math.log(2.0)
    assert gnss.value == pytest.approx(4.17 + (4.04 - 4.17) * t, abs=1e-9)
    assert gnss.freq_hz == 1.575e9 and gnss.warnings == ()
    far = mat.resolve_dk(lib, "isola.370hr", freq_hz=5e10, policy="design")
    assert far.value == 3.92 and ids_of(far) == ["ST-W6"] and far.freq_hz == 1e10


def test_point_values_cover_half_a_decade(lib):
    near = mat.resolve_dk(lib, "generic.fr4", freq_hz=2.45e9, policy="design")
    assert near.value == 4.5 and near.warnings == ()
    wifi = mat.resolve_dk(lib, "generic.fr4", freq_hz=5.8e9, policy="design")
    assert wifi.value == 4.5 and ids_of(wifi) == ["ST-W6"]


def test_policy_falls_back_through_the_order(lib):
    typ = mat.resolve_dk(lib, "rogers.ro4350b", freq_hz=1e10, policy="typical")
    assert (typ.value, typ.basis) == (3.48, "process")    # no typical: process is next
    bond = mat.resolve_dk(lib, "rogers.ro4450f", freq_hz=1e10, policy="design")
    assert (bond.value, bond.basis) == (3.52, "process")


def test_construction_and_resin_content_pick_the_most_specific_entry(lib):
    m6 = mat.resolve_dk(lib, "panasonic.megtron6", freq_hz=1e9, policy="design",
                        construction="1078", rc_pct=63)
    assert (m6.value, m6.basis) == (3.49, "typical")
    m6_13g = mat.resolve_dk(lib, "panasonic.megtron6", freq_hz=1.3e10, policy="design",
                            construction="1078", rc_pct=63)
    assert m6_13g.value == 3.43
    laminate = mat.resolve_dk(lib, "panasonic.megtron6", freq_hz=1e9, policy="design")
    assert laminate.value == 3.71


def test_explicit_dk_wins_and_needs_no_library_entry(lib):
    rv = mat.resolve_dk(lib, "rogers.ro4350b", freq_hz=1e9, explicit=3.5, explicit_freq_hz=1e10)
    assert (rv.value, rv.basis, rv.freq_hz, rv.source, rv.warnings) == \
        (3.5, "explicit", 1e10, "explicit", ())
    label = mat.resolve_dk(lib, "acme.magic", freq_hz=1e9, explicit=4.0)
    assert (label.value, label.basis, label.freq_hz) == (4.0, "explicit", 1e9)
    with pytest.raises(LookupError, match="acme.magic"):
        mat.resolve_dk(lib, "acme.magic", freq_hz=1e9)


def test_df_resolution(lib):
    assert mat.resolve_df(lib, "rogers.ro4350b", freq_hz=2.5e9, policy="design").value == 0.0031
    assert mat.resolve_df(lib, "rogers.ro4350b", freq_hz=1e10, policy="design").value == 0.0037
    mid = mat.resolve_df(lib, "rogers.ro4350b", freq_hz=5e9, policy="design")
    expected = 0.0031 + (0.0037 - 0.0031) * math.log(2.0) / math.log(4.0)
    assert mid.value == pytest.approx(expected, abs=1e-12)
    lora = mat.resolve_df(lib, "rogers.ro4350b", freq_hz=9.15e8, policy="design")
    assert lora.value == 0.0031 and lora.freq_hz == 2.5e9 and lora.warnings == ()
    assert mat.resolve_df(lib, "nanya.np155f", freq_hz=1e9, policy="fab", fab="jlcpcb",
                          construction="7628").value == 0.015


@pytest.mark.parametrize("kwargs, needle", [
    ({"freq_hz": 1e9, "policy": "cheapest"}, "policy"),
    ({"freq_hz": 0.0}, "freq_hz"),
    ({"freq_hz": -1.0}, "freq_hz"),
    ({"freq_hz": 1e9, "explicit": -3.5}, "explicit dk"),
    ({"freq_hz": 1e9, "explicit": 3.5, "explicit_freq_hz": -1.0}, "explicit_freq_hz"),
])
def test_bad_arguments_are_rejected(lib, kwargs, needle):
    with pytest.raises(ValueError, match=needle):
        mat.resolve_dk(lib, "rogers.ro4350b", **kwargs)


def test_resolved_value_is_frozen_and_serialisable(lib):
    rv = mat.resolve_dk(lib, "rogers.ro4350b", freq_hz=9.15e8, policy="design",
                        thickness_mm=0.254)
    with pytest.raises(AttributeError):
        rv.value = 1.0                                   # type: ignore[misc]
    d = rv.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert d["value"] == 3.66 and {w["id"] for w in d["warnings"]} == {"ST-W5", "ST-W6"}
    assert str(rv.warnings[0]).startswith("ST-W")


def test_generic_mask_is_typical_lpi(lib):
    m = mat.generic_mask(lib)
    assert m["material"] == "lpi" and m["dk"] == 3.8 and m["df"] == 0.025
    assert (m["over_substrate"], m["over_trace"], m["between_traces"]) == (0.0305, 0.0152, 0.0305)
    assert m["basis"] == "typical"
    m["dk"] = 1.0
    assert mat.generic_mask(lib)["dk"] == 3.8


# ---------------------------------------------------------------------------
# 4. stdlib only (venv without site-packages, and KiCad 9's bundled Python)
# ---------------------------------------------------------------------------
def test_materials_imports_only_the_stdlib():
    path = TOOLS / "materials.py"
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    assert not {n for n in names if n not in sys.stdlib_module_names}
    assert len(path.read_text(encoding="utf-8").splitlines()) < 800


CHILD = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "{quiet}"
    "before = set(sys.modules)\n"
    "import materials as m\n"
    "lib = m.load_library()\n"
    "rv = m.resolve_dk(lib, 'rogers.ro4350b', freq_hz=9.15e8, policy='design',"
    " thickness_mm=0.254)\n"
    "new = sorted(x for x in set(sys.modules) - before\n"
    "             if x.split('.')[0] not in sys.stdlib_module_names and x != 'materials')\n"
    "print(json.dumps({{'py': list(sys.version_info[:2]), 'v': rv.value,"
    " 'w': [w.id for w in rv.warnings], 'dv': lib.data_version, 'new': new}}))\n")


def test_materials_runs_with_no_site_packages(lib):
    proc = subprocess.run([sys.executable, "-I", "-S", "-c", CHILD.format(quiet=""), str(TOOLS)],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          timeout=LIVE_TIMEOUT_S, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["new"] == [] and out["v"] == 3.66 and sorted(out["w"]) == ["ST-W5", "ST-W6"]
    assert out["dv"] == lib.data_version


@pytest.fixture(scope="module")
def kicad9_python() -> Path:
    import kicad_env as env
    for inst in env.discover():
        if inst.version_tuple[0] == 9 and inst.python is not None:
            return Path(inst.python)
    pytest.skip("no KiCad 9 bundled Python")


def test_live_materials_imports_under_kicad9_python(kicad9_python, tmp_path, lib):
    child = tmp_path / "child.py"
    child.write_text(CHILD.format(quiet="import kicad_quiet\n"), encoding="utf-8")
    proc = subprocess.run([str(kicad9_python), str(child), str(TOOLS)], capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, timeout=LIVE_TIMEOUT_S,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["py"][0] == 3 and out["py"][1] == 11
    assert out["new"] == [] and out["v"] == 3.66 and sorted(out["w"]) == ["ST-W5", "ST-W6"]
    assert out["dv"] == lib.data_version
