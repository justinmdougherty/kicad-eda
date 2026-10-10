"""D7-23a: the board tools on the physical-design artifact (ADR D7.4 board-side lookup, D7.9
DSN layer policy and the T10 plane-class fallback, D7.12 fab outputs).

server/board_physics.py holds the logic; board_build / board_autoroute / board_fab /
board_pipeline_tools only call it. Unit tier, no KiCad: tc.run, tc.kicad_python, the KiCad
install and the board-session release are faked (as in tests/test_autoroute_canon.py and
tests/test_board_pipeline_outline.py); the artifacts are real (physics_resolve, fd off).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import board_build
import board_physics as bp
import board_pipeline_tools as bpt
import board_route_tools as brt
import decisions
import dsn_canon as dc
import dsn_layers as dl
import emc_stitch
import fab_stackup as fs
import kicad_env as env
import kicad_ipc as ipc
import netclass_writer
import physics_resolve as pr
import swig_stackup as ss
import toolchain as tc
from conftest import load_example

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server"
FIX = HERE / "fixtures"
CP_DSN = (FIX / "dsn_layers" / "cp2102n.dsn.txt").read_text(encoding="utf-8")
K9_6L = FIX / "fab_stackup" / "k9_6l"
K9_BOARD = ('(kicad_pcb\n\t(version 20241229)\n\t(generator "pcbnew")\n'
            '\t(generator_version "9.0")\n)\n')
SIX = {"preset": "jlcpcb:JLC06161H-2116", "impedance_control": {"required": True},
       "rationale": "6L: two inner routing layers"}
GND_PAD = {"type": "unconnected_items", "severity": "error", "items": [
    {"description": "Pad 2 [GND] of C1 on F.Cu"}, {"description": "Zone [GND] on In1.Cu"}]}
TXD_PADS = {"type": "unconnected_items", "severity": "error", "items": [
    {"description": "Pad 3 [TXD] of U1 on F.Cu"}, {"description": "Pad 2 [TXD] of J2 on F.Cu"}]}
STAMP = {"id": "dec-000000000000", "ts": "2026-10-10T00:00:00+00:00"}
MonkeyPatch = pytest.MonkeyPatch


def _fixture(folder: str, name: str) -> dict:
    return json.loads((FIX / folder / f"{name}.json").read_text(encoding="utf-8"))["design"]


def _write_json(path: Path, doc: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return path


def _cp2102n() -> dict:
    d = load_example("cp2102n_uart")
    return {**d, "nets": {n: [p for p in c if p != ["J1", "SH"]] for n, c in d["nets"].items()}}


def _artifact(design: dict, netlist: Path, board: Path, **dsn: object) -> Path:
    """<board stem>.stackup.json of `design` (physics_resolve, fd off); `dsn` overrides keys
    of its dsn section."""
    art = pr.resolve_design(design, netlist, board_path=board, fd="off")
    art = {**art, "dsn": {**art["dsn"], **dsn}}
    path = bp.board_artifact(board)
    path.write_text(pr.artifact_text(art), encoding="ascii")
    return path


# --- lookup ----------------------------------------------------------------------------------
def test_constants_match_their_owners() -> None:
    assert bp.SCHEMA == pr.SCHEMA and bp.ARTIFACT_SUFFIX == pr.ARTIFACT_SUFFIX
    assert bp.ENV_VAR == ss.ARTIFACT_ENV and (bp.RESTRICTED, bp.UNRESTRICTED) == dl.VARIANTS
    assert bp.STITCH_FLAG == emc_stitch.STACKUP_FLAG
    assert bp.netlist_artifact("p/netlist.json") == pr.artifact_path("p/netlist.json")
    assert bp.board_artifact("o/board.kicad_pcb") == Path("o/board.stackup.json")


def test_artifact_for_explicit_then_netlist_then_board(tmp_path: Path) -> None:
    net, pcb = tmp_path / "proj" / "netlist.json", tmp_path / "out" / "board.kicad_pcb"
    assert bp.artifact_for(netlist=str(net)) is None and bp.artifact_for(pcb=str(pcb)) is None
    side = _write_json(tmp_path / "out" / "board.stackup.json", {})
    near = _write_json(tmp_path / "proj" / "netlist.stackup.json", {})
    other = _write_json(tmp_path / "x.json", {})
    assert bp.artifact_for(pcb=str(pcb)) == str(side)
    assert bp.artifact_for(netlist=str(net), pcb=str(pcb)) == str(near)
    assert bp.artifact_for(pcb=str(pcb), explicit=str(other)) == str(other)
    with pytest.raises(ValueError, match="not a file"):
        bp.artifact_for(pcb=str(pcb), explicit=str(tmp_path / "missing.json"))


def test_find_refuses_a_bad_artifact(tmp_path: Path) -> None:
    pcb = str(tmp_path / "board.kicad_pcb")
    assert bp.find(pcb) is None
    _write_json(tmp_path / "board.stackup.json", {"schema": "other"})
    bad = bp.find(pcb)
    assert isinstance(bad, dict) and bad["ok"] is False and pr.SCHEMA in bad["reason"]
    good = _write_json(tmp_path / "good.json", {"schema": pr.SCHEMA, "dsn": None})
    found = bp.find(pcb, str(good))
    assert isinstance(found, bp.Found) and found.path == str(good)


# --- ensure_artifact -------------------------------------------------------------------------
@pytest.fixture
def six(tmp_path: Path) -> tuple[Path, Path]:
    """(proj/netlist.json declaring the ADR 6L stackup, build/board.kicad_pcb)."""
    design = {**_fixture("stackups", "d722_6l_2116"), "stackup": SIX}
    (tmp_path / "build").mkdir()
    return _write_json(tmp_path / "proj" / "netlist.json", design), tmp_path / "build" / \
        "board.kicad_pcb"


def _ensure(netlist: Path, board: Path) -> dict:
    design = json.loads(netlist.read_text(encoding="utf-8"))
    return bp.ensure_artifact(str(netlist), str(board), design=design)


def test_a_declared_stackup_is_resolved_with_dru_and_mirror(six: tuple[Path, Path]) -> None:
    netlist, board = six
    res = _ensure(netlist, board)["physics"]
    art = netlist.with_name("netlist.stackup.json")
    assert res["artifact"] == os.path.abspath(art) and res["resolved"] is True
    assert res["mirror"] == os.path.abspath(board.with_name("board.stackup.json"))
    assert Path(res["mirror"]).read_bytes() == art.read_bytes()            # byte-identical
    dru = board.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    assert "kicad-eda managed begin" in dru and "kicad_eda_plane_In4.Cu" in dru
    assert res["dru_path"] == os.path.abspath(board.with_suffix(".kicad_dru"))
    assert res["decisions_written"] == 2 and res["errors"] == []
    assert pr.staleness(art, json.loads(netlist.read_text()), board_path=board) is None


def test_a_fresh_artifact_is_reused_and_a_stale_one_resolved(
        six: tuple[Path, Path], monkeypatch: MonkeyPatch) -> None:
    netlist, board = six
    _ensure(netlist, board)
    calls: list[str] = []
    real = pr.write_artifact
    monkeypatch.setattr(pr, "write_artifact", lambda *a, **k: calls.append("w") or real(*a, **k))
    again = _ensure(netlist, board)["physics"]
    assert again["resolved"] is False and again["stale_reason"] is None and calls == []
    design = json.loads(netlist.read_text(encoding="utf-8"))
    _write_json(netlist, {**design, "net_specs": {"SIG": {"current": 1.0}}})
    stale = _ensure(netlist, board)["physics"]
    assert stale["resolved"] is True and "design digest" in stale["stale_reason"]
    assert calls == ["w"]
    assert Path(stale["mirror"]).read_bytes() == netlist.with_name(
        "netlist.stackup.json").read_bytes()


def test_the_same_stem_beside_the_netlist_needs_no_mirror(tmp_path: Path) -> None:
    design = {**_fixture("stackups", "d722_6l_2116"), "copper_layers": 6}
    netlist = _write_json(tmp_path / "netlist.json", design)
    res = _ensure(netlist, tmp_path / "netlist.kicad_pcb")["physics"]
    assert res["mirror"] is None and res["artifact"].endswith("netlist.stackup.json")


def test_an_undeclared_design_without_an_artifact_keeps_todays_build(tmp_path: Path) -> None:
    netlist = _write_json(tmp_path / "proj" / "netlist.json", load_example("lm358_buffer"))
    res = _ensure(netlist, tmp_path / "proj" / "board.kicad_pcb")
    assert res == {"physics": {"artifact": None, "note": bp.NOT_DECLARED}}
    assert [p.name for p in (tmp_path / "proj").iterdir()] == ["netlist.json"]


def test_an_undeclared_rebuild_removes_a_stale_board_mirror(tmp_path: Path) -> None:
    netlist = _write_json(tmp_path / "proj" / "netlist.json", load_example("lm358_buffer"))
    stale = _write_json(tmp_path / "out" / "board.stackup.json", {"schema": pr.SCHEMA})
    res = _ensure(netlist, tmp_path / "out" / "board.kicad_pcb")["physics"]
    assert res["removed"] == os.path.abspath(stale) and not stale.exists()


def test_an_existing_artifact_is_kept_fresh_for_an_undeclared_design(tmp_path: Path) -> None:
    netlist = _write_json(tmp_path / "netlist.json", load_example("lm358_buffer"))
    pr.write_artifact(netlist, board_path=tmp_path / "netlist.kicad_pcb", fd="off")
    res = _ensure(netlist, tmp_path / "out.kicad_pcb")["physics"]
    assert res["resolved"] is True and "rules file" in res["stale_reason"]   # the new board
    assert (tmp_path / "out.kicad_dru").is_file() and (tmp_path / "out.stackup.json").is_file()


def test_stackup_errors_refuse_the_build(tmp_path: Path) -> None:
    netlist = _write_json(tmp_path / "netlist.json", _fixture("stackups", "st_e1"))
    res = _ensure(netlist, tmp_path / "board.kicad_pcb")
    assert res["ok"] is False and "ST-E1" in res["reason"] and "nothing was built" in res["reason"]


def test_builder_env_carries_only_the_ensured_artifact() -> None:
    base = {"PATH": "x", "kicad_eda_stackup": "stray", "PACK_SCALE": "1.0"}
    assert bp.builder_env(base, {"physics": {"artifact": None}}) == {"PATH": "x",
                                                                     "PACK_SCALE": "1.0"}
    assert bp.builder_env(base, {"physics": {"artifact": "a.json"}})[bp.ENV_VAR] == "a.json"


# --- the build hook: call order --------------------------------------------------------------
class Builder:
    """tc.run standing in for swig_build_board: records argv, env and the board folder."""

    def __init__(self) -> None:
        self.runs: list[dict] = []

    def __call__(self, args: list, **kw: object) -> subprocess.CompletedProcess:
        out = Path(str(args[3]))
        self.runs.append({"args": [str(a) for a in args], "env": dict(kw.get("env") or {}),
                          "present": sorted(p.name for p in out.parent.iterdir())})
        result = {"ok": True, "zones": ["In1.Cu:GND", "In4.Cu:+3V3"], "warnings": []}
        return subprocess.CompletedProcess(args, 0, json.dumps(result) + "\n", "")


@pytest.fixture
def builder(tmp_path: Path, monkeypatch: MonkeyPatch) -> Builder:
    fps = tmp_path / "stock" / "footprints"
    fps.mkdir(parents=True)
    fake = Builder()
    monkeypatch.delenv(bp.ENV_VAR, raising=False)
    monkeypatch.setattr(tc, "kicad_python", lambda: "kpy")
    monkeypatch.setattr(tc, "footprints_dir", lambda explicit="": str(fps))
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": str(tmp_path / "stock"))
    monkeypatch.setattr(tc, "run", fake)
    monkeypatch.setattr(ipc, "write_guard", lambda path: None)
    monkeypatch.setattr(board_build, "release_board_session", lambda pcb: {})
    return fake


def _spy_netclasses(monkeypatch: MonkeyPatch) -> list[dict]:
    seen: list[dict] = []

    def spy(pro: str, design: dict, cls: int = 3, *a: object, **kw: object) -> dict:
        seen.append(kw)
        return {"ok": True, "class_profile": "c3", "classes_written": []}
    monkeypatch.setattr(netclass_writer, "apply_to_project", spy)
    return seen


def test_the_artifact_dru_and_mirror_exist_when_the_builder_starts(
        builder: Builder, six: tuple[Path, Path], monkeypatch: MonkeyPatch) -> None:
    netlist, board = six
    seen = _spy_netclasses(monkeypatch)
    res = bpt.build_board_from_netlist(str(netlist), 30, 20, out_pcb=str(board))
    assert res["ok"] is True, res
    [run] = builder.runs
    assert {"board.kicad_dru", "board.stackup.json"} <= set(run["present"])
    art = os.path.abspath(netlist.with_name("netlist.stackup.json"))
    assert run["env"][bp.ENV_VAR] == art == res["physics"]["artifact"]
    assert res["physics"]["mirror"] == os.path.abspath(board.with_name("board.stackup.json"))
    assert seen == [{"plane_nets": (), "artifact": art}]          # roles, not zone strings


def test_an_undeclared_build_gets_no_artifact_and_no_stray_env(
        builder: Builder, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv(bp.ENV_VAR, str(tmp_path / "stray.stackup.json"))
    seen = _spy_netclasses(monkeypatch)
    netlist = _write_json(tmp_path / "proj" / "netlist.json", load_example("lm358_buffer"))
    res = board_build.build(str(netlist), 30, 20, out_pcb=str(tmp_path / "proj" / "b.kicad_pcb"))
    assert res["ok"] is True and res["physics"]["note"] == bp.NOT_DECLARED
    assert bp.ENV_VAR not in builder.runs[0]["env"]
    assert seen == [{"plane_nets": ("GND", "+3V3"), "artifact": None}]     # today's planes


def test_artifact_errors_refuse_before_the_builder(builder: Builder, tmp_path: Path) -> None:
    netlist = _write_json(tmp_path / "netlist.json", _fixture("stackups", "st_e1"))
    res = board_build.build(str(netlist), 30, 20)
    assert res["ok"] is False and "ST-E1" in res["reason"] and builder.runs == []


# --- autoroute -------------------------------------------------------------------------------
@pytest.fixture
def machine(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """KiCad 9.0.4 with its pcbnew Python, Java and a jar; an empty session registry."""
    root = tmp_path / "kicad"
    stable = env.KiCadInstall(version="9.0", version_tuple=(9, 0, 4), root=root,
                              kicad_cli=root / "bin" / "kicad-cli.exe",
                              python=root / "bin" / "python.exe", share=root / "share",
                              has_api_server=False)
    monkeypatch.setattr(env, "discover", lambda refresh=False: [stable])
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(tc, "find_java", lambda: "java")
    monkeypatch.setattr(tc, "freerouting_jar", lambda explicit="": "freerouting-1.9.0.jar")
    monkeypatch.setattr(ipc, "write_guard", lambda path: None)
    monkeypatch.setattr(brt, "_BOARDS", {})
    monkeypatch.setattr(brt, "_PROGRESS", {})


class Chain:
    """tc.run standing in for DSN export -> Freerouting -> SES import -> DRC -> emc_stitch.
    `drc` is the unconnected items of each DRC run in turn (then clean)."""

    def __init__(self, drc: list[list[dict]] | None = None) -> None:
        self.drc = list(drc or [])
        self.calls: list[list[str]] = []
        self.routed: list[str] = []
        self.boards: list[str] = []

    def __call__(self, args: list, **kw: object) -> subprocess.CompletedProcess:
        args = [str(a) for a in args]
        self.calls.append(args)
        joined = " ".join(args)
        if "swig_export_dsn.py" in joined:
            Path(args[3]).write_text(CP_DSN, encoding="utf-8")
        elif "-jar" in args:
            self.routed.append(Path(args[args.index("-de") + 1]).read_text(encoding="utf-8"))
            Path(args[args.index("-do") + 1]).write_text("(session out)", encoding="utf-8")
        elif "swig_import_ses.py" in joined:
            board = Path(args[2])
            self.boards.append(board.read_text(encoding="utf-8"))
            board.write_text(self.boards[-1] + f"# route {len(self.boards)}\n", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, '{"ok": true, "tracks": 9, "vias": 1, '
                                                        '"warnings": []}\n', "")
        elif "emc_stitch.py" in joined:
            return subprocess.CompletedProcess(args, 0, '{"ok": true, "vias_added": 4}\n', "")
        elif "drc" in args:
            items = self.drc.pop(0) if self.drc else []
            Path(args[args.index("-o") + 1]).write_text(
                json.dumps({"violations": [], "unconnected_items": items}), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, "", "")

    def of(self, script: str) -> list[list[str]]:
        return [a for a in self.calls if any(script in x for x in a)]


@pytest.fixture
def board(tmp_path: Path) -> Path:
    pcb = tmp_path / "netlist.kicad_pcb"
    pcb.write_text(K9_BOARD, encoding="utf-8")
    return pcb


def _chain(monkeypatch: MonkeyPatch, drc: list[list[dict]] | None = None) -> Chain:
    chain = Chain(drc)
    monkeypatch.setattr(tc, "run", chain)
    return chain


def _policy_text(policy: dict, variant: str | None = None) -> str:
    return dc.canonicalize(dl.postprocess(CP_DSN, policy, variant=variant)[0], name="netlist.dsn")


def test_without_an_artifact_the_route_is_todays(machine: None, board: Path,
                                                 monkeypatch: MonkeyPatch) -> None:
    chain = _chain(monkeypatch)
    res = bpt.autoroute_board(str(board))
    assert res["ok"] is True and "dsn_policy" not in res, res
    assert chain.routed == [dc.canonicalize(CP_DSN, name="netlist.dsn")]
    assert [len(a) for a in chain.of("swig_import_ses.py")] == [4]          # no argv[3]
    assert res["physics"] == {"artifact": None, "note": bp.NO_ARTIFACT}


def test_the_artifact_shapes_the_dsn_and_the_ses_import(machine: None, board: Path,
                                                        monkeypatch: MonkeyPatch) -> None:
    art = _artifact(_cp2102n(), board.with_suffix(".json"), board)
    policy = json.loads(art.read_text(encoding="utf-8"))["dsn"]
    chain = _chain(monkeypatch)
    res = bpt.autoroute_board(str(board))
    assert res["ok"] is True and res["unconnected"] == 0, res
    assert chain.routed == [_policy_text(policy)]
    assert res["dsn_sha256"] == hashlib.sha256(chain.routed[0].encode("utf-8")).hexdigest()
    assert res["dsn_policy"]["nets_moved"] == {"+3V3": "kicad_eda_plane_+3V3",
                                               "GND": "kicad_eda_plane_GND"}
    assert res["dsn_policy"]["artifact"] == str(art) and res["physics"]["artifact"] == str(art)
    assert chain.of("swig_import_ses.py")[0][-1] == str(art)
    assert "fallback" not in res["dsn_policy"] and not list(board.parent.glob("*.preroute"))


def test_an_explicit_stackup_json_wins_and_a_missing_one_refuses(
        machine: None, board: Path, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    elsewhere = tmp_path / "elsewhere" / "netlist.kicad_pcb"
    elsewhere.parent.mkdir()
    art = _artifact(_cp2102n(), elsewhere.with_suffix(".json"), elsewhere)
    chain = _chain(monkeypatch)
    res = bpt.autoroute_board(str(board), stackup_json=str(art))
    assert res["ok"] is True and chain.of("swig_import_ses.py")[0][-1] == str(art)
    monkeypatch.setattr(tc, "run", lambda args, **kw: pytest.fail(f"ran {args[:2]}"))
    gone = bpt.autoroute_board(str(board), stackup_json=str(tmp_path / "none.json"))
    assert gone["ok"] is False and gone["step"] == "dsn" and "not a file" in gone["reason"]


def test_pad_layer_excluded_stops_at_step_dsn(machine: None, board: Path,
                                              monkeypatch: MonkeyPatch) -> None:
    _artifact(_cp2102n(), board.with_suffix(".json"), board,
              net_layers={"USB_DP": {"primary": "B.Cu", "widths": {"B.Cu": 0.3}}})
    chain = _chain(monkeypatch)
    res = bpt.autoroute_board(str(board))
    assert res["ok"] is False and res["step"] == "dsn", res
    assert res["pad_layer_excluded"] == {"USB_DP": ["F.Cu"]} and "USB_DP" in res["reason"]
    assert chain.routed == [] and not chain.of("swig_import_ses.py")


def test_a_policy_that_does_not_fit_the_board_stops_at_step_dsn(
        machine: None, board: Path, monkeypatch: MonkeyPatch) -> None:
    _artifact(_cp2102n(), board.with_suffix(".json"), board,
              signal_layers=["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"])
    chain = _chain(monkeypatch)
    res = bpt.autoroute_board(str(board))
    assert res["ok"] is False and res["step"] == "dsn" and "does not fit" in res["reason"]
    assert chain.routed == []


def test_a_plane_pad_left_unconnected_reroutes_once_unrestricted(
        machine: None, board: Path, monkeypatch: MonkeyPatch) -> None:
    art = _artifact(_cp2102n(), board.with_suffix(".json"), board)
    policy = json.loads(art.read_text(encoding="utf-8"))["dsn"]
    chain = _chain(monkeypatch, drc=[[GND_PAD], []])
    res = bpt.autoroute_board(str(board))
    assert chain.routed == [_policy_text(policy), _policy_text(policy, bp.UNRESTRICTED)]
    assert chain.boards == [K9_BOARD, K9_BOARD]                  # re-routed from the pre-route
    assert res["ok"] is True and res["unconnected"] == 0, res
    fb = res["dsn_policy"]["fallback"]
    assert res["dsn_policy"]["variant"] == bp.UNRESTRICTED and fb["kept"] == bp.UNRESTRICTED
    assert (fb["unconnected_before"], fb["unconnected_after"]) == (1, 0)
    assert fb["plane_unconnected_before"] == {"GND": 1} and "GND (1)" in fb["reason"]
    [draft] = res["decision_drafts"]
    assert (draft["stage"], draft["confidence"]) == ("G4", "med")
    assert decisions.validate_decision({**draft, **STAMP}) == []
    assert sorted(p.name for p in board.parent.iterdir() if "kicad_pcb" in p.name) == [
        "netlist.kicad_pcb"]


def test_a_worse_fallback_keeps_the_restricted_route(machine: None, board: Path,
                                                     monkeypatch: MonkeyPatch) -> None:
    _artifact(_cp2102n(), board.with_suffix(".json"), board)
    chain = _chain(monkeypatch, drc=[[GND_PAD], [GND_PAD, TXD_PADS]])
    res = bpt.autoroute_board(str(board))
    assert len(chain.routed) == 2 and res["unconnected"] == 1 and res["ok"] is False
    assert res["dsn_policy"]["variant"] == bp.RESTRICTED
    assert res["dsn_policy"]["fallback"]["kept"] == bp.RESTRICTED
    assert board.read_text(encoding="utf-8") == K9_BOARD + "# route 1\n"   # the first route


def test_a_reroute_that_raises_restores_the_restricted_route(
        machine: None, board: Path, monkeypatch: MonkeyPatch) -> None:
    _artifact(_cp2102n(), board.with_suffix(".json"), board)
    chain = _chain(monkeypatch, drc=[[GND_PAD]])

    def flaky(args: list, **kw: object) -> subprocess.CompletedProcess:
        if "-jar" in [str(a) for a in args] and chain.routed:
            raise OSError("java vanished")
        return chain(args, **kw)
    monkeypatch.setattr(tc, "run", flaky)
    with pytest.raises(OSError, match="java vanished"):
        bpt.autoroute_board(str(board))
    assert board.read_text(encoding="utf-8") == K9_BOARD + "# route 1\n"
    assert sorted(p.name for p in board.parent.iterdir() if "kicad_pcb" in p.name) == [
        "netlist.kicad_pcb"]


@pytest.mark.parametrize("items", [[TXD_PADS], []], ids=["signal-net", "clean"])
def test_no_fallback_without_an_unconnected_plane_pad(machine: None, board: Path,
                                                      monkeypatch: MonkeyPatch,
                                                      items: list[dict]) -> None:
    _artifact(_cp2102n(), board.with_suffix(".json"), board)
    chain = _chain(monkeypatch, drc=[items])
    res = bpt.autoroute_board(str(board))
    assert len(chain.routed) == 1 and "fallback" not in res["dsn_policy"]
    assert "decision_drafts" not in res


def test_plane_unconnected_reads_pad_nets_of_the_fresh_report(tmp_path: Path) -> None:
    pcb = tmp_path / "b.kicad_pcb"
    report = {"unconnected_items": [GND_PAD, TXD_PADS, GND_PAD, {"items": "junk"}, 7]}
    _write_json(tmp_path / "b_drc.json", report)
    policy = {"dsn": {"plane_nets": {"GND": ["In1.Cu"], "+3V3": ["In2.Cu"]}}}
    assert bp.plane_unconnected(str(pcb), policy) == {"GND": 2}
    assert bp.plane_unconnected(str(tmp_path / "none.kicad_pcb"), policy) == {}


# --- add_ground_stitching --------------------------------------------------------------------
def test_stitching_follows_the_artifact(machine: None, board: Path,
                                        monkeypatch: MonkeyPatch) -> None:
    chain = _chain(monkeypatch)
    plain = bpt.add_ground_stitching(str(board))
    assert plain["ok"] is True and plain["physics"]["artifact"] is None
    assert not any(a.startswith(bp.STITCH_FLAG) for a in chain.of("emc_stitch.py")[0])
    art = _artifact(_cp2102n(), board.with_suffix(".json"), board)
    chain = _chain(monkeypatch)
    res = bpt.add_ground_stitching(str(board))
    assert res["ok"] is True and res["physics"] == {"artifact": str(art)}
    assert f"{bp.STITCH_FLAG}={art}" in chain.of("emc_stitch.py")[0]
    gone = bpt.add_ground_stitching(str(board), stackup_json=str(board.parent / "x.json"))
    assert gone["ok"] is False and "not a file" in gone["reason"]


# --- export_fab_package ----------------------------------------------------------------------
def _fab_cli(monkeypatch: MonkeyPatch, ipc_rc: int = 0) -> list[list[str]]:
    """A kicad-cli that writes the KiCad 9.0.4 6L job file + copper Gerbers (s6L)."""
    calls: list[list[str]] = []

    def run(args: list[str], **kw: object) -> subprocess.CompletedProcess:
        calls.append(list(args))
        target = Path(args[args.index("-o") + 1])
        if args[3] == "gerbers":
            target.mkdir(parents=True, exist_ok=True)
            for f in K9_6L.iterdir():
                shutil.copyfile(f, target / f.name)
        elif args[3] in ("drill", "pos"):
            (target / "s6L.drl" if args[3] == "drill" else target).write_text("x", "utf-8")
        elif args[3] == "ipc2581" and ipc_rc == 0:
            target.write_text("<IPC-2581/>", encoding="utf-8")
        return subprocess.CompletedProcess(args, ipc_rc if args[3] == "ipc2581" else 0, "",
                                           "Error: no\n" if ipc_rc else "")
    monkeypatch.setattr(tc, "board_cli", lambda pcb="": "kicad-cli")
    monkeypatch.setattr(tc, "run", run)
    return calls


def _s6l(tmp_path: Path) -> Path:
    pcb = tmp_path / "s6L.kicad_pcb"
    pcb.write_text(K9_BOARD, encoding="utf-8")
    _artifact(_fixture("fab_stackup", "s6l_k9"), tmp_path / "s6L.json", pcb)
    return pcb


def test_the_zip_carries_the_fixed_job_file_ipc2581_csv_and_notes(
        tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    pcb = _s6l(tmp_path)
    calls = _fab_cli(monkeypatch)
    res = bpt.export_fab_package(str(pcb), str(tmp_path / "fab"))
    assert res["ok"] is True, res
    assert [c[3] for c in calls] == ["gerbers", "drill", "pos", "ipc2581"]
    stack = res["fab_stackup"]
    assert stack["problems"] == [] and stack["gbrjob"]["rows"] == 14 == stack["gbrjob"][
        "expected_rows"]
    assert len(stack["gbrjob"]["changed"]) == 4 and "6 copper layers" in " ".join(
        stack["ipc2581"]["reasons"])
    with zipfile.ZipFile(res["zip"]) as z:
        names = set(z.namelist())
        job = json.loads(z.read("s6L-job.gbrjob"))
    assert {"s6L_ipc2581.xml", "s6L_stackup.csv", "s6L_fab_notes.md"} <= names
    copper = [e["FileFunction"] for e in job["FilesAttributes"]
              if e["FileFunction"].startswith("Copper,")]
    assert copper == ["Copper,L1,Top", "Copper,L2,Inr", "Copper,L3,Inr", "Copper,L4,Inr",
                      "Copper,L5,Inr", "Copper,L6,Bot"]
    assert fs.check_gbrjob(tmp_path / "fab" / "s6L-job.gbrjob",
                           json.loads(pcb.with_suffix(".stackup.json").read_text()))["ok"]


def test_a_failed_ipc2581_export_fails_the_package(tmp_path: Path,
                                                   monkeypatch: MonkeyPatch) -> None:
    pcb = _s6l(tmp_path)
    _fab_cli(monkeypatch, ipc_rc=1)
    res = bpt.export_fab_package(str(pcb), str(tmp_path / "fab"))
    assert res["ok"] is False and "IPC-2581 export failed" in res["reason"]
    assert res["fab_stackup"]["ipc2581"]["path"] is None


def test_without_an_artifact_the_fab_package_is_unchanged(tmp_path: Path,
                                                          monkeypatch: MonkeyPatch) -> None:
    pcb = tmp_path / "s6L.kicad_pcb"
    pcb.write_text(K9_BOARD, encoding="utf-8")
    calls = _fab_cli(monkeypatch)
    res = bpt.export_fab_package(str(pcb), str(tmp_path / "fab"))
    assert [c[3] for c in calls] == ["gerbers", "drill", "pos"] and "fab_stackup" not in res
    gone = bpt.export_fab_package(str(pcb), stackup_json=str(tmp_path / "x.json"))
    assert gone["ok"] is False and "not a file" in gone["reason"]


# --- structure -------------------------------------------------------------------------------
@pytest.mark.parametrize("name, limit", [("board_pipeline_tools", 500), ("board_build", 400),
                                         ("board_autoroute", 400), ("board_fab", 400),
                                         ("board_physics", 800)])
def test_files_stay_small(name: str, limit: int) -> None:
    assert (SERVER / f"{name}.py").read_text(encoding="utf-8").count("\n") < limit


def test_board_physics_keeps_server_tools_off_sys_path(tmp_path: Path) -> None:
    code = ("import os, sys\nsys.path.insert(0, sys.argv[1])\nimport board_physics\n"
            "tools = os.path.normcase(os.path.abspath(sys.argv[2]))\n"
            "print(sum(1 for p in sys.path if p and os.path.normcase(os.path.abspath(p)) == "
            "tools))\n")
    child = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    r = subprocess.run([sys.executable, "-c", code, str(SERVER), str(SERVER / "tools")],
                       cwd=tmp_path, env=child, stdin=subprocess.DEVNULL, capture_output=True,
                       encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0 and r.stdout.strip().splitlines()[-1] == "0", r.stderr[-800:]
