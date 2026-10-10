"""swig_layer_role_checks (ADR D7.8 R8 / R27 / R28; task D7-18a) on real boards.

venv tier (always runs): the CLI's usage and no-pcbnew errors (one JSON line, exit 2 / 1).

live tier (KiCad 9.0.4 locally, 10.0.5 under the K10 recipe; skipped without a pcbnew
Python): tests/fixtures/boards/role_checks/build.py builds the synthetic boards (exactly one
foreign track on the In1 GND plane and its clean twin; a GND -> +3V3 layer transition with a
capacitor 1.7 mm away, none, and one 6 mm away; a 6L dual stripline with an 8 mm parallel
run) and the script is run on them as the harness runs it (KiCad's Python, stdin closed,
last stdout line JSON). The committed cp2102n_pre_d7 board is today's pre-D7 routed
cp2102n_uart (build.py's docstring says how it was made): its foreign-track count is pinned.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import stackup_model as sm
import swig_stackup as ss
from fixtures.swig_python import TOOLS, kicad_footprints, require_kicad_python

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "boards" / "role_checks"
SCRIPT = TOOLS / "swig_layer_role_checks.py"
BUILDER = FIXTURES / "build.py"
SYNTHETIC = ("foreign_4l", "clean_4l", "transition_cap", "transition_nocap",
             "transition_farcap", "dual_6l")
CP2102N = "cp2102n_pre_d7"
# Pinned: today's flow left 16 foreign track segments on the plane layers of the committed
# board (In1.Cu GND: USB_DM 3, VBUS 5, VBUS_IN 4; In2.Cu +3V3: RST 2, USB_DM 2).
CP2102N_FOREIGN = 16
CP2102N_BY_LAYER = {"In1.Cu": {"USB_DM": 3, "VBUS": 5, "VBUS_IN": 4},
                    "In2.Cu": {"RST": 2, "USB_DM": 2}}


def _last_json(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no JSON object line in:\n{stdout}")


def _run(python: str, *args: object, timeout: int = 300) -> tuple[int, dict, str]:
    proc = subprocess.run([python, str(SCRIPT), *map(str, args)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=timeout)
    output = f"--- stdout\n{proc.stdout}\n--- stderr\n{proc.stderr}"
    return proc.returncode, _last_json(proc.stdout), output


# ---------------------------------------------------------------- venv tier
def test_usage_error_is_one_json_line_and_exit_2() -> None:
    rc, res, out = _run(sys.executable)
    assert rc == 2 and res["ok"] is False and "usage" in res["error"], out


def test_without_pcbnew_the_script_fails_cleanly(tmp_path: Path) -> None:
    netlist = tmp_path / "n.json"
    shutil.copyfile(FIXTURES / "role4l.json", netlist)
    rc, res, out = _run(sys.executable, tmp_path / "missing.kicad_pcb", netlist)
    assert rc == 1 and res["ok"] is False and "pcbnew" in res["error"], out


# ---------------------------------------------------------------- live tier
@pytest.fixture(scope="module")
def kpy() -> str:
    return require_kicad_python()


@pytest.fixture(scope="module")
def built(kpy: str, tmp_path_factory: pytest.TempPathFactory) -> dict:
    work = tmp_path_factory.mktemp("role_checks")
    cap_dir = kicad_footprints(kpy, "Capacitor_SMD.pretty")
    proc = subprocess.run([kpy, str(BUILDER), str(work), str(TOOLS), str(cap_dir)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=900)
    assert proc.returncode == 0, f"builder exited {proc.returncode}\n{proc.stderr}"
    res = _last_json(proc.stdout)
    for name in SYNTHETIC:
        board = res["boards"][name]
        assert "error" not in board, board.get("error")
        assert board["finalize"]["ok"], (name, board["finalize"])
    return {**res, "work": work}


def _check(kpy: str, built: dict, name: str, *extra: object) -> dict:
    board = built["boards"][name]
    rc, res, out = _run(kpy, board["pcb"], board["netlist"], *extra)
    assert rc == 0 and res["ok"] is True, out
    return res


def test_the_builder_and_the_checks_run_on_the_same_kicad(kpy: str, built: dict) -> None:
    res = _check(kpy, built, "clean_4l")
    assert res["pcbnew"] == built["version"]
    assert res["pcbnew"].split(".")[0] in ("9", "10"), res["pcbnew"]
    assert res["board_copper"] == res["stackup_copper"] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert res["stackup"] == {"source": "resolved", "artifact": None, "note": None}


def test_synthetic_foreign_counts_are_exactly_one_and_zero(kpy: str, built: dict) -> None:
    foreign = _check(kpy, built, "foreign_4l")
    plane = foreign["metrics"]["plane_layers"]
    assert plane["foreign_tracks"] == 1 and plane["foreign_zones"] == 0
    assert plane["by_layer"]["In1.Cu"]["nets"] == {"SIG_A": 1}
    assert plane["by_layer"]["In2.Cu"]["foreign_tracks"] == 0
    assert len(foreign["issues"]) == 1 and foreign["issues"][0].startswith("R8: In1.Cu")
    clean = _check(kpy, built, "clean_4l")
    assert clean["metrics"]["plane_layers"]["foreign_tracks"] == 0
    assert clean["issues"] == [] and clean["warnings"] == []


def test_a_gnd_to_3v3_transition_needs_a_capacitor_within_2mm(kpy: str, built: dict) -> None:
    cap = _check(kpy, built, "transition_cap")
    assert cap["warnings"] == [] and cap["issues"] == []
    assert cap["metrics"]["ref_transitions"] == {"impedance_vias": 1, "reference_changes": 1,
                                                 "unsupported": 0, "unjudged_vias": 0}
    for name in ("transition_nocap", "transition_farcap"):
        res = _check(kpy, built, name)
        assert res["issues"] == [] and len(res["warnings"]) == 1, (name, res["warnings"])
        w = res["warnings"][0]
        assert w.startswith("R27: Z50 changes layer F.Cu -> B.Cu") and "GND -> +3V3" in w
        assert res["metrics"]["ref_transitions"]["unsupported"] == 1


def test_an_8mm_dual_stripline_parallel_run_warns_once(kpy: str, built: dict) -> None:
    res = _check(kpy, built, "dual_6l")
    assert res["issues"] == [] and len(res["warnings"]) == 1
    assert res["warnings"][0].startswith("R28: ZS on In2.Cu") and "AUX on In3.Cu" in \
        res["warnings"][0]
    runs = res["metrics"]["dual_stripline"]["parallel_runs"]
    assert [(r["other_net"], r["over_layer"]) for r in runs] == [("AUX", "In3.Cu")]
    assert runs[0]["length_mm"] == pytest.approx(8.0, abs=1e-3)
    assert res["metrics"]["dual_stripline"]["dual_stripline_lines"] == 1


def _cp2102n(tmp_path: Path) -> tuple[Path, Path]:
    pcb, netlist = tmp_path / f"{CP2102N}.kicad_pcb", tmp_path / f"{CP2102N}.json"
    shutil.copyfile(FIXTURES / f"{CP2102N}.kicad_pcb", pcb)
    shutil.copyfile(FIXTURES / f"{CP2102N}.json", netlist)
    return pcb, netlist


def test_the_pre_d7_cp2102n_board_reports_its_pinned_foreign_count(kpy: str,
                                                                  tmp_path: Path) -> None:
    pcb, netlist = _cp2102n(tmp_path)
    rc, res, out = _run(kpy, pcb, netlist)
    assert rc == 0 and res["ok"] is True, out
    plane = res["metrics"]["plane_layers"]
    assert plane["foreign_tracks"] == CP2102N_FOREIGN >= 1
    assert {k: v["nets"] for k, v in plane["by_layer"].items()} == CP2102N_BY_LAYER
    assert plane["plane_layers"] == {"In1.Cu": "GND", "In2.Cu": "+3V3"}
    assert [i.split(" ")[1] for i in res["issues"]] == ["In1.Cu", "In2.Cu"]
    rc2, again, _ = _run(kpy, pcb, netlist)
    assert rc2 == 0 and json.dumps(again, sort_keys=True) == json.dumps(res, sort_keys=True)


def _artifact(path: Path, design_path: Path) -> Path:
    design = json.loads(design_path.read_text(encoding="utf-8"))
    rs, issues = sm.resolve(design)
    assert rs is not None, issues
    path.write_text(json.dumps({"schema": ss.ARTIFACT_SCHEMA, "stackup": rs.to_dict()}),
                    encoding="utf-8")
    return path


def test_roles_come_from_a_fresh_artifact_and_a_stale_one_is_flagged(kpy: str, built: dict,
                                                                     tmp_path: Path) -> None:
    netlist = Path(built["boards"]["foreign_4l"]["netlist"])
    fresh = _artifact(tmp_path / "fresh.stackup.json", netlist)
    res = _check(kpy, built, "foreign_4l", fresh)
    assert res["stackup"]["source"] == "artifact" and res["stackup"]["note"] is None
    assert res["metrics"]["plane_layers"]["foreign_tracks"] == 1
    stale = _artifact(tmp_path / "stale.stackup.json", FIXTURES / "dual6l.json")
    res = _check(kpy, built, "foreign_4l", stale)
    assert res["stackup"]["source"] == "resolved" and "stale" in res["stackup"]["note"]
    assert res["metrics"]["plane_layers"]["foreign_tracks"] == 1


def test_a_board_whose_copper_is_not_the_stackups_is_a_blocker(kpy: str, built: dict,
                                                               tmp_path: Path) -> None:
    netlist = tmp_path / "six.json"
    shutil.copyfile(FIXTURES / "dual6l.json", netlist)
    rc, res, out = _run(kpy, built["boards"]["clean_4l"]["pcb"], netlist)
    assert rc == 0 and res["ok"] is True, out
    assert res["issues"][0].startswith("R8: the board's copper (F.Cu, In1.Cu, In2.Cu, B.Cu)")


def test_an_unreadable_board_is_an_error_not_a_pass(kpy: str, tmp_path: Path) -> None:
    netlist = tmp_path / "n.json"
    shutil.copyfile(FIXTURES / "role4l.json", netlist)
    rc, res, out = _run(kpy, tmp_path / "missing.kicad_pcb", netlist)
    assert rc == 1 and res["ok"] is False and "missing.kicad_pcb" in res["error"], out
    broken = tmp_path / "broken.kicad_pcb"
    broken.write_text("(kicad_pcb (version 99999999)", encoding="utf-8")
    rc, res, out = _run(kpy, broken, netlist)
    assert rc == 1 and res["ok"] is False, out
