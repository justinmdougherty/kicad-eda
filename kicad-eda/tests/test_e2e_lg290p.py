"""E2E (F2-19): examples/lg290p_breakout, a Quectel LG290P RTK GNSS breakout
built on a project library, through the gated pipeline to DRC 0 and Gerbers.

The example is native design (ROADMAP D1): the LG290P footprint and symbol
are authored by the MCP tools (library_tools.author_footprint /
author_symbol, preset 'lg290p'), which write libs/lg290p_breakout_parts.*
and register them in the netlist's 'libraries' block; the 55 inner GND pads
reach the In1.Cu plane through the preset's netlist_hints (F2-S1 strategy
(f), lga_strategy.apply_hints at U1), never hand-copied preroutes.

Tiers:
  * contract (always): the netlist, the marker that joins pipeline/run_all.py,
    the library files against the preset renders, the preroutes against the
    preset hints, the route hash against the footprint bytes;
  * MCP regeneration (KiCad installed): the example rebuilt from its base
    through the real tools is identical to the committed one;
  * pipeline (KiCad + Java + Freerouting): run_pipeline end to end on a copy
    (schematic on the KiCad 11 api backend when the nightly is installed,
    else the file backend; board on the SWIG KiCad), PASS with DRC 0,
    unconnected 0, Gerbers, BOM and CPL; the board FPID carries the project
    nickname; ERC has no lib_symbol_issues (a KiCad configuration without
    the stock global tables, as in the bare K10 container, may only name the
    stock libraries it lacks: the ERC is then repeated with KiCad's default
    tables in a temporary KICAD_CONFIG_HOME, never the user's config); the
    report lists U1's libraries as 'generated'; a pad edit in the .kicad_mod
    misses the route cache and the board is rebuilt and re-routed with the
    new pad.
"""
from __future__ import annotations

import contextlib
import copy
import json
import re
import shutil
from pathlib import Path
from typing import Any, Iterator

import pytest

import decisions
import fp_author
import fp_author_templates as ft
import kicad_env
import lga_strategy
import netlist_schema
import part_presets
import rp_hooks
import rp_route_hash
import run_all
import sym_author
import toolchain as tc

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
NAME = "lg290p_breakout"
EXAMPLE = EXAMPLES / NAME
NETLIST = EXAMPLE / "netlist.json"
MARKER = EXAMPLE / run_all.MARKER
NICK = f"{NAME}_parts"
FP_NAME = ft.LG290P_NAME
FPID = f"{NICK}:{FP_NAME}"
SYM_ID = f"{NICK}:LG290P"
LIBS_DIR = "libs"
SYM_REL = f"{LIBS_DIR}/{NICK}.kicad_sym"
FP_LIB_REL = f"{LIBS_DIR}/{NICK}.pretty"
SYM_FILE = EXAMPLE / SYM_REL
FP_FILE = EXAMPLE / FP_LIB_REL / f"{FP_NAME}.kicad_mod"
BOARD_W, BOARD_H = 48.0, 32.0
LID_MM, KEEP_CLEAR_MM = 2.8, 3.0
GENERATED_KEYS = ("libraries", "preroutes")     # what the tools + the hints add to the base
GND_PADS = part_presets.PRESET_GND_PADS["lg290p"]
RESERVED_PADS = frozenset(n for n, f in ft.LG290P_PAD_FUNCTIONS.items() if f == "RESERVED")
SWIG_FORMATS = (20241229, 20260206)             # KiCad 9.0 / 10.0 boards (RULES)
LIBRARY_FINDINGS = ("lib_symbol_issues", "footprint_link_issues")
UNCONFIGURED_LIBRARY = re.compile(r"does not include the (symbol|footprint) library '[^']+'")
EDITED_PAD, EDITED_SIZE = "1", (1.4, 0.8)       # pad 1 (RESERVED, N/C): 1.5 -> 1.4 mm long
CACHE_MISS_HASH = "placement/routing inputs changed since the cached run"


# --- helpers ---------------------------------------------------------------------------------
def _design() -> dict:
    return json.loads(NETLIST.read_text(encoding="utf-8"))


def _comp(design: dict, ref: str) -> dict:
    return next(c for c in design["components"] if c["ref"] == ref)


def _on(design: dict, net: str) -> set[tuple[str, str]]:
    return {(ref, str(pad)) for ref, pad in design["nets"][net]}


def _u1_net_pads(design: dict) -> list[str]:
    return [str(p) for pins in design["nets"].values() for r, p in pins if r == "U1"]


def base_design(design: dict) -> dict:
    """The example before the MCP tools and the hints ran: no libraries
    block, no (generated) preroutes."""
    return {k: copy.deepcopy(v) for k, v in design.items() if k not in GENERATED_KEYS}


def copy_example(dest: Path) -> Path:
    """examples/lg290p_breakout's sources (netlist.json + libs/) in ``dest``;
    returns the copied netlist.json."""
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(NETLIST, dest / NETLIST.name)
    shutil.copytree(EXAMPLE / LIBS_DIR, dest / LIBS_DIR)
    return dest / NETLIST.name


def edit_pad_size(kicad_mod: Path, pad: str, size: tuple[float, float]) -> None:
    """Rewrite the (size w h) of pad ``pad`` in a footprint file."""
    text = kicad_mod.read_text(encoding="utf-8")
    m = re.search(rf'\(pad "{re.escape(pad)}" [^\n]*\n(?:[^\n]*\n)*?\s*\(size [^)]*\)', text)
    assert m, f"pad {pad} not found in {kicad_mod}"
    new_block = re.sub(r"\(size [^)]*\)", f"(size {size[0]:g} {size[1]:g})", m.group(0))
    kicad_mod.write_text(text[:m.start()] + new_block + text[m.end():], encoding="utf-8")


@contextlib.contextmanager
def plain_run_env() -> Iterator[None]:
    """The environment of a plain pipeline run: every discovered feature (no
    env filter, no test override) and the route cache on."""
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("PIPELINE_NO_CACHE", raising=False)
        mp.delenv(rp_hooks.ENV_VAR, raising=False)
        before = rp_hooks.selection()
        rp_hooks.set_features(None)
        try:
            yield
        finally:
            rp_hooks.set_features(before)


@pytest.fixture
def discovery_features() -> Iterator[None]:
    with plain_run_env():
        yield


# --- contract tier ---------------------------------------------------------------------------
def test_marker_joins_the_regression_matrix() -> None:
    entry = run_all.read_entry(MARKER)
    assert entry.problems == ()
    assert entry.marker == run_all.Marker(w=BOARD_W, h=BOARD_H, expect="PASS")
    assert NAME in [e.name for e in run_all.discover(EXAMPLES)]
    assert NAME not in [b[0] for b in run_all.BOARDS]          # joined by marker, not BOARDS


def test_netlist_declares_the_project_library() -> None:
    design = _design()
    assert design["name"] == NAME
    assert design["libraries"] == {"symbols": {NICK: SYM_REL}, "footprints": {NICK: FP_LIB_REL}}
    u1 = _comp(design, "U1")
    assert (u1["lib_id"], u1["footprint"], u1["value"]) == (SYM_ID, FPID, "LG290P")
    assert SYM_FILE.is_file() and FP_FILE.is_file()


def test_all_59_gnd_pads_on_gnd_and_reserved_pads_no_connect() -> None:
    design = _design()
    gnd = {p for r, p in _on(design, "GND") if r == "U1"}
    assert len(GND_PADS) == 59 and gnd == GND_PADS
    groups = sym_author.pin_groups(sym_author.lg290p_spec(FPID))
    assert set(groups["GND"]) == GND_PADS                       # the symbol's stacked pins
    nc = [str(p) for r, p in design["no_connect"] if r == "U1"]
    assert RESERVED_PADS == {"1", "2", "5", "17"} and RESERVED_PADS <= set(nc)
    netted = _u1_net_pads(design)
    pads = {p.number for p in ft.lg290p().pads if p.number}
    assert len(pads) == 79
    assert sorted(netted + nc, key=int) == sorted(pads, key=int)   # each pad exactly once


def test_rf_input_chain() -> None:
    design = _design()
    assert "U.FL" in _comp(design, "J1")["footprint"]
    assert {("J1", "1"), ("C1", "1"), ("L1", "1"), ("D1", "1")} <= _on(design, "ANT")
    assert _on(design, "RF_IN") == {("C1", "2"), ("U1", "11")}       # 100 pF DC block
    assert _comp(design, "C1")["value"] == "100pF"
    assert {("R1", "2"), ("U1", "9")} <= _on(design, "VDD_RF")         # active-antenna bias
    assert {("R1", "1"), ("L1", "2")} <= _on(design, "VBIAS")
    assert (_comp(design, "R1")["value"], _comp(design, "L1")["value"]) == ("10", "68nH")
    assert _comp(design, "D1")["lib_id"] == "Device:D_TVS" and ("D1", "2") in _on(design, "GND")
    assert design["net_specs"]["ANT"]["impedance"] == 50
    assert design["net_specs"]["RF_IN"]["impedance"] == 50


def test_power_header_and_leds() -> None:
    design = _design()
    assert _comp(design, "U2")["lib_id"] == "Regulator_Linear:AP2112K-3.3"
    assert {("U2", "5"), ("U1", "23")} <= _on(design, "+3V3")         # LDO out -> VCC
    assert {("U2", "1"), ("J2", "1")} <= _on(design, "VIN")
    caps = {r for r, _ in _on(design, "+3V3") if r.startswith("C")}
    assert {_comp(design, r)["value"] for r in caps} >= {"100nF", "10uF"}   # decoupling
    bckp = _on(design, "V_BCKP")
    assert ("U1", "22") in bckp and any(r.startswith("C") for r, _ in bckp)
    j2 = _comp(design, "J2")
    assert j2["lib_id"] == "Connector_Generic:Conn_01x06"
    on_j2 = {net for net, pins in design["nets"].items() if any(r == "J2" for r, _ in pins)}
    assert on_j2 == {"VIN", "GND", "TXD1", "RXD1", "PPS", "RESET_N"}
    assert len([c for c in design["components"] if c["lib_id"] == "Device:LED"]) == 3


def test_height_zones_and_keep_clear() -> None:
    design = _design()
    zones = design["height_zones"]
    lid = next(z for z in zones if z["max_height"] == LID_MM)
    assert (lid["x"], lid["y"], lid["w"], lid["h"]) == (0, 0, BOARD_W, BOARD_H)
    body, u1 = ft.lg290p().body, _comp(design, "U1")
    keep = next(z for z in zones if z.get("exempt") == ["U1"])
    assert keep["max_height"] == 0.0 and keep["side"] == "both"
    assert keep["w"] == pytest.approx(body.w + 2 * KEEP_CLEAR_MM)
    assert keep["h"] == pytest.approx(body.h + 2 * KEEP_CLEAR_MM)
    assert keep["x"] + keep["w"] / 2 == pytest.approx(u1["x"])
    assert keep["y"] + keep["h"] / 2 == pytest.approx(u1["y"])
    assert u1["height"] == body.height == LID_MM


def test_every_part_carries_an_mpn() -> None:
    design = _design()
    assert design.get("require_sourcing") is True                 # G5 blocks on a missing one
    assert [c["ref"] for c in design["components"] if not c.get("mpn")] == []


def test_schema_and_requirement_assumptions() -> None:
    design = _design()
    assert netlist_schema.validate(design) == []
    assert decisions.validate_assumptions(design["assumptions"]) == []
    topics = {a["id"] for a in design["assumptions"]}
    assert {"board.copper_layers", "gnss.inner_gnd", "mech.keep_clear"} <= topics


def test_preroutes_are_the_presets_netlist_hints() -> None:
    design = _design()
    hints = part_presets.preset_hints("lg290p")
    expected = lga_strategy.apply_hints(base_design(design), hints, ref="U1")
    assert design["preroutes"] == expected["preroutes"]
    vias = [v for pr in design["preroutes"] for v in pr.get("vias", ())]
    assert (len(design["preroutes"]), len(vias)) == (80, 40)
    assert "planes" not in design and "emc" not in design         # strategy (f) only


def test_library_files_are_the_preset_renders(tmp_path: Path) -> None:
    assert FP_FILE.read_text(encoding="utf-8") == fp_author.render(ft.lg290p())
    rendered = tmp_path / SYM_FILE.name
    sym_author.author_symbol(sym_author.lg290p_spec(FPID), rendered)
    assert SYM_FILE.read_text(encoding="utf-8") == rendered.read_text(encoding="utf-8")
    assert f'(property "Footprint" "{FPID}"' in SYM_FILE.read_text(encoding="utf-8")


@pytest.mark.usefixtures("discovery_features")
def test_route_hash_follows_the_footprint_bytes(tmp_path: Path) -> None:
    netlist = copy_example(tmp_path / NAME)
    design = json.loads(netlist.read_text(encoding="utf-8"))
    before = rp_route_hash.route_hash(design, netlist.parent)
    assert rp_route_hash.route_hash({**design, "title": "renamed"}, netlist.parent) == before
    kicad_mod = netlist.parent / FP_LIB_REL / FP_FILE.name
    original = kicad_mod.read_bytes()
    edit_pad_size(kicad_mod, EDITED_PAD, EDITED_SIZE)
    assert kicad_mod.read_bytes() != original
    assert rp_route_hash.route_hash(design, netlist.parent) != before
    kicad_mod.write_bytes(original)
    assert rp_route_hash.route_hash(design, netlist.parent) == before


# --- MCP regeneration tier (KiCad validates the authored parts) ------------------------------
def _kicad_missing() -> str | None:
    if not tc.kicad_cli():
        return "kicad-cli not found"
    if not tc.kicad_python():
        return f"no KiCad Python with pcbnew: {tc.kicad_python_error()}"
    return None


def test_example_regenerates_through_the_mcp_tools(tmp_path: Path) -> None:
    missing = _kicad_missing()
    if missing:
        pytest.skip(missing)
    import library_tools
    committed = _design()
    netlist = tmp_path / NAME / NETLIST.name
    netlist.parent.mkdir()
    netlist.write_text(json.dumps(base_design(committed), indent=2), encoding="utf-8")
    fp = library_tools.author_footprint(str(netlist), preset="lg290p")
    assert fp["ok"], fp.get("reason")
    assert (fp["lib_id"], fp["pads"], fp["registered"]) == (FPID, 79, True)
    assert fp["validation"]["problems"] == []
    assert fp["netlist_hints"] == part_presets.preset_hints("lg290p")
    sym = library_tools.author_symbol(str(netlist), preset="lg290p")
    assert sym["ok"], sym.get("reason")
    assert (sym["lib_id"], sym["footprint"], sym["pins"]) == (SYM_ID, FPID, 79)
    authored = json.loads(netlist.read_text(encoding="utf-8"))
    assert lga_strategy.apply_hints(authored, fp["netlist_hints"], ref="U1") == committed
    for rel in (SYM_REL, f"{FP_LIB_REL}/{FP_FILE.name}"):
        assert (netlist.parent / rel).read_bytes() == (EXAMPLE / rel).read_bytes(), rel


# --- pipeline tier ----------------------------------------------------------------------------
def _pipeline_missing() -> str | None:
    missing = _kicad_missing()
    if missing:
        return missing
    if not tc.find_java():
        return "no Java runtime for Freerouting"
    if not tc.freerouting_jar():
        return "Freerouting jar not found"
    return None


def _run(netlist: Path) -> dict:
    import run_pipeline as rp
    return rp.run(str(netlist), BOARD_W, BOARD_H, route=run_all.ROUTE)


@pytest.fixture(scope="module")
def e2e(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """One fresh pipeline run of a copy of the example (no cache manifest)."""
    missing = _pipeline_missing()
    if missing:
        pytest.skip(missing)
    netlist = copy_example(tmp_path_factory.mktemp("e2e") / NAME)
    sources = netlist.read_bytes()
    with plain_run_env():
        report = _run(netlist)
    yield {"netlist": netlist, "report": report, "sources": sources}


def _stem(netlist: Path) -> Path:
    return netlist.with_suffix("")


def assert_passed(report: dict, netlist: Path) -> None:
    assert report["PASS"], report.get("gate_summary") or report.get("error")
    assert [(g["gate"], g["passed"]) for g in report["gates"]] == [
        (f"G{i}", True) for i in range(1, 6)]
    assert (report["drc"]["errors"], report["drc"]["unconnected"]) == (0, 0)
    fab = report["fab"]
    assert fab["ok"] and fab["bom"] and fab["cpl"]
    out = Path(str(_stem(netlist)) + "_gerbers")
    assert (out / fab["zip"]).is_file() or (netlist.parent / fab["zip"]).is_file()
    assert [p for p in out.iterdir() if p.suffix.lower() in (".gbr", ".gtl", ".gbl")]
    for name in ("BOM.csv", "CPL.csv"):
        assert "U1" in (out / name).read_text(encoding="utf-8"), name


def test_pipeline_passes_with_drc_0_and_a_fab_package(e2e: dict) -> None:
    report, netlist = e2e["report"], e2e["netlist"]
    assert_passed(report, netlist)
    assert report["route_cache"] == "miss"                       # a fresh run routed it
    assert report["sourcing"]["missing"] == [] and report["sourcing"]["bad"] == []
    assert netlist.read_bytes() == e2e["sources"]               # no remediation rewrote it


def test_schematic_backend(e2e: dict) -> None:
    import kicad_server as k
    sch = e2e["report"]["schematic"]
    expected = "api" if k._api_available() else "file"         # nightly locally, K10 in Docker
    assert sch["backend"] == expected, sch
    assert sch.get("fallback") is None
    assert sch["erc_ok"] and sch["erc_violations"] == 0


def test_board_fpid_carries_the_project_nickname(e2e: dict) -> None:
    pcb = Path(str(_stem(e2e["netlist"])) + ".kicad_pcb")
    text = pcb.read_text(encoding="utf-8")
    assert f'(footprint "{FPID}"' in text
    assert tc.board_file_info(str(pcb))["format_version"] in SWIG_FORMATS


def _erc_library_findings(sch: str, rpt: Path) -> list[tuple[str, str, list[str]]]:
    """(type, description, item descriptions) of every lib_symbol_issues /
    footprint_link_issues violation of an all-severity ERC (newest kicad-cli)."""
    erc = tc.erc_json(sch, str(rpt), severity_error=False)
    assert "report" in erc, erc.get("erc_error")
    return [(v.get("type"), v.get("description", ""),
             [i.get("description", "") for i in v.get("items", [])])
            for s in erc["report"].get("sheets", []) for v in s.get("violations", [])
            if v.get("type") in LIBRARY_FINDINGS]


def _default_kicad_config(tmp_path: Path) -> Path:
    """A KICAD_CONFIG_HOME holding the install's default global library tables
    (<share>/template/{sym,fp}-lib-table, what KiCad's first run sets up)."""
    version = kicad_env.cli_version(Path(tc.kicad_cli())) or ""
    major, minor, _ = kicad_env.parse_version(version)
    assert major, f"kicad-cli version unknown ({version!r})"
    home = tmp_path / "kicad_config"
    (home / f"{major}.{minor}").mkdir(parents=True)
    for name in ("sym-lib-table", "fp-lib-table"):
        shutil.copy2(Path(tc.kicad_share()) / "template" / name, home / f"{major}.{minor}" / name)
    return home


def test_erc_has_no_lib_symbol_issues(e2e: dict, tmp_path: Path) -> None:
    """No library finding touches the project library; a KiCad configuration
    without the stock global tables (a bare container) only reports the stock
    libraries it lacks, and with KiCad's default tables nothing is left."""
    sch = str(_stem(e2e["netlist"])) + ".kicad_sch"
    found = _erc_library_findings(sch, tmp_path / "erc.json")
    assert [f for f in found if NICK in f[1]
            or any(item.startswith("Symbol U1 ") for item in f[2])] == [], found
    assert all(UNCONFIGURED_LIBRARY.search(desc) for _, desc, _ in found), found
    if found:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("KICAD_CONFIG_HOME", str(_default_kicad_config(tmp_path)))
            found = _erc_library_findings(sch, tmp_path / "erc_default_tables.json")
    assert found == [], found


def test_report_libraries_show_the_generated_source(e2e: dict) -> None:
    report = e2e["report"]
    generated = {"symbol": "generated", "footprint": "generated"}
    assert report["libraries"]["U1"] == generated
    assert report["schematic"]["libraries"]["U1"] == generated
    assert report["libraries"]["C1"] == {"symbol": "stock", "footprint": "stock"}
    review = Path(str(_stem(e2e["netlist"])) + "_review.md").read_text(encoding="utf-8")
    assert "generated" in review


def _route_cache_state(netlist: Path) -> tuple[str, str | None]:
    """(route_cache, its miss reason) the run would decide now, through the
    harness's own stage on a fresh context (a hit removes nothing)."""
    import rp_context
    import rp_stage_place
    ctx = rp_context.RunContext(netlist=str(netlist), w=BOARD_W, h=BOARD_H)
    ctx.design = json.loads(netlist.read_text(encoding="utf-8"))
    assert rp_stage_place.stage_route_cache(ctx)
    return ctx.report["route_cache"], ctx.report.get("route_cache_reason")


def _pad_sizes(pcb: Path, pad: str) -> set[tuple[float, float]]:
    text = pcb.read_text(encoding="utf-8")
    u1 = text[text.index(f'(footprint "{FPID}"'):]
    u1 = u1[:u1.find("\n\t(footprint ", 1) if "\n\t(footprint " in u1 else len(u1)]
    found = re.findall(rf'\(pad "{re.escape(pad)}" [^\n]*\n(?:[^\n]*\n)*?\s*\(size ([\d.]+) '
                       rf'([\d.]+)\)', u1)
    return {(float(w), float(h)) for w, h in found}


def test_footprint_edit_forces_a_reroute(e2e: dict) -> None:
    netlist = e2e["netlist"]
    pcb = Path(str(_stem(netlist)) + ".kicad_pcb")
    with plain_run_env():
        assert _route_cache_state(netlist) == ("hit", None)        # unchanged: reusable
        assert _pad_sizes(pcb, EDITED_PAD) == {(1.5, 0.8)}
        edit_pad_size(netlist.parent / FP_LIB_REL / FP_FILE.name, EDITED_PAD, EDITED_SIZE)
        rerun = _run(netlist)
    assert rerun["route_cache"] == "miss"
    assert rerun["route_cache_reason"] == CACHE_MISS_HASH
    assert rerun["autoroute"].get("tracks")                     # Freerouting ran again
    assert_passed(rerun, netlist)
    assert _pad_sizes(pcb, EDITED_PAD) == {EDITED_SIZE}         # rebuilt with the edited pad
