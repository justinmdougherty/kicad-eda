"""Live tier of dru_writer (D7-12a): the managed rules compile and each 2a family
flags exactly its violation on a real board (ADR D7.8, rules R6 / R7 / R8).

- Compile: render() of the probe rules and of both snapshot cases passes
  drc_guard.rules_compile (rc 0, fresh report, the canary fired, no canary added)
  on KiCad 9.0.4, 10.0.5 (the RULES.md K10 Docker recipe) and 10.99.
- Families: tests/fixtures/dru/build_probe.py draws probe.json's 4-layer board
  under KiCad's bundled Python (an In1.Cu GND zone, a foreign SIG track on In1.Cu,
  an RF50 track 0.30 mm against the 0.3546..0.3654 mm window, an RF50 track on
  the disallowed B.Cu, plus clean control tracks). The board's DRC with no rules
  file is the baseline; with only R8, only R7 or only R6 written by write(), the
  DRC (--severity-all, drc_guard.run_drc) differs from the baseline by exactly ONE
  error of the expected type on the expected track (net, layer and start point);
  with all three over a user rules file, by exactly the three, and the user text
  stays byte-identical. Required on 9.0.4 and 10.0.5; also run on 10.99 (the
  KiCad 11 agent path's route_check DRC reads this file).

Skips cleanly without the release's kicad-cli or without a KiCad Python that
imports pcbnew (the probe board cannot be built).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

import drc_guard as dg
import dru_writer as dw
import kicad_env as env
import stackup_model as sm
import toolchain as tc

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures" / "dru"
TOOLS = HERE.parent / "server" / "tools"
PROBE = json.loads((FIX / "probe.json").read_text(encoding="utf-8"))
RELEASES = {"9.0": (9, 0), "10.0": (10, 0), "10.99": (10, 99)}
FAMILIES = ("R8", "R7", "R6")
CASES = ("probe", "jlc4l_usb", "six_layer")
K10_RECIPE = "RULES.md K10 (kicad-eda:wave1, KiCad 10.0.5)"
BUILD_TIMEOUT_S = 300
USER_RULES = ("(version 1)\r\n# user rules kept by kicad-eda\r\n"
              "(rule \"user_keep\" (condition \"A.NetName == 'NO_SUCH_NET'\")\r\n"
              "  (constraint track_width (min 5mm)))\r\n")

Key = tuple[str, str, tuple[tuple[str, float, float], ...]]


@pytest.fixture(scope="module")
def installs() -> list[env.KiCadInstall]:
    return env.discover()


def _cli(installs: list[env.KiCadInstall], release: str) -> str:
    for inst in installs:
        if inst.version_tuple[:2] == RELEASES[release]:
            return str(inst.kicad_cli)
    where = K10_RECIPE if release == "10.0" else "this machine"
    pytest.skip(f"no KiCad {release} kicad-cli installed (run on {where})")


def _rules(case: str, family: str | None = None) -> list[dw.Rule]:
    spec = PROBE if case == "probe" else json.loads((FIX / f"{case}.json").read_text("utf-8"))
    rs, issues = sm.resolve(spec["design"])
    assert rs is not None, [str(i) for i in issues]
    rules = dw.base_rules(rs, spec["lines"], pairs=dw.pairs_of(spec["design"]))
    return [r for r in rules if family is None or dw.family_of(r) == family]


@pytest.fixture(scope="module")
def probe_board(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """probe.json's board, built once by KiCad's bundled Python (stdin closed)."""
    kpy = tc.kicad_python()
    if not kpy:
        pytest.skip(f"no KiCad pcbnew Python to build the probe: {tc.kicad_python_error()}")
    out = tmp_path_factory.mktemp("dru_probe") / "probe.kicad_pcb"
    proc = subprocess.run([kpy, str(FIX / "build_probe.py"), str(FIX / "probe.json"), str(out),
                           str(TOOLS)], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL, timeout=BUILD_TIMEOUT_S)
    assert proc.returncode == 0, f"build_probe.py exited {proc.returncode}\n{proc.stdout}\n" \
                                 f"{proc.stderr}"
    info = json.loads(proc.stdout.strip().splitlines()[-1])
    assert (info["layers"], info["tracks"], info["zones"], info["filled"]) == \
        (4, len(PROBE["board"]["tracks"]), 1, True), info
    return out


def _copy(board: Path, dest: Path) -> Path:
    dest.mkdir(parents=True)
    copy = dest / board.name
    shutil.copyfile(board, copy)
    shutil.copyfile(board.with_suffix(".kicad_pro"), copy.with_suffix(".kicad_pro"))
    return copy


def _key(violation: dict) -> Key:
    items = tuple(sorted((str(i.get("description", "")), float(i["pos"]["x"]),
                          float(i["pos"]["y"])) for i in violation.get("items", [])))
    return str(violation.get("type")), str(violation.get("severity")), items


def _drc(board: Path, cli: str) -> tuple[Counter[Key], list[dict]]:
    res = dg.run_drc(str(board), cli, severity="all")
    assert "error" not in res and res["rc"] == 0, res
    report = json.loads(Path(res["report"]).read_text(encoding="utf-8"))
    return Counter(_key(v) for v in report["violations"]), report["violations"]


def _expected(family: str) -> Key:
    want = PROBE["expect"][family]
    track = next(t for t in PROBE["board"]["tracks"] if t["id"] == want["track"])
    x, y = (o + d for o, d in zip(PROBE["board"]["origin"], track["from"], strict=True))
    length = abs(track["to"][0] - track["from"][0]) + abs(track["to"][1] - track["from"][1])
    item = (f"Track [{track['net']}] on {track['layer']}, length {length:.4f} mm",
            float(x), float(y))
    return want["type"], "error", (item,)


_BASELINES: dict[str, Counter[Key]] = {}


def _baseline(board: Path, cli: str, tmp_path: Path) -> Counter[Key]:
    if cli not in _BASELINES:
        _BASELINES[cli] = _drc(_copy(board, tmp_path / "baseline"), cli)[0]
    return _BASELINES[cli]


def _new_violations(board: Path, cli: str, rules: list[dw.Rule], tmp_path: Path,
                    user: str | None = None) -> tuple[Counter[Key], list[dict], Path]:
    base = _baseline(board, cli, tmp_path)
    work = _copy(board, tmp_path / "rules")
    if user is not None:
        dw.dru_path_for(work).write_bytes(user.encode("utf-8"))
    res = dw.write(work, rules)
    assert res["rules"] == len(rules) + 1
    found, raw = _drc(work, cli)
    assert not base - found, f"violations vanished with the rules: {base - found}"
    return found - base, raw, Path(res["path"])


# ---------------------------------------------------------------- compile
@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("release", list(RELEASES))
def test_generated_files_compile(installs: list[env.KiCadInstall], release: str,
                                 case: str) -> None:
    text = dw.render(_rules(case))
    res = dg.rules_compile(text, _cli(installs, release))
    assert res["ok"] is True and res["rc"] == 0 and res["fired"] >= 1, res
    assert res["canary_added"] is False and res["crash"] is False, res


# ---------------------------------------------------------------- families
@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("release", list(RELEASES))
def test_each_family_gives_exactly_its_violation(installs: list[env.KiCadInstall],
                                                 probe_board: Path, release: str, family: str,
                                                 tmp_path: Path) -> None:
    cli = _cli(installs, release)
    rules = _rules("probe", family)
    assert rules, family
    new, raw, _ = _new_violations(probe_board, cli, rules, tmp_path)
    assert new == Counter({_expected(family): 1}), new
    if PROBE["expect"][family]["type"] == "items_not_allowed":      # the report names the rule
        hits = [v for v in raw if v["type"] == "items_not_allowed"]
        assert len(hits) == 1 and any(r.name in hits[0]["description"] for r in rules), hits


@pytest.mark.parametrize("release", list(RELEASES))
def test_all_families_over_user_rules(installs: list[env.KiCadInstall], probe_board: Path,
                                      release: str, tmp_path: Path) -> None:
    cli = _cli(installs, release)
    new, _, dru = _new_violations(probe_board, cli, _rules("probe"), tmp_path, user=USER_RULES)
    assert new == Counter({_expected(f): 1 for f in FAMILIES}), new
    text = dru.read_bytes().decode("utf-8")
    assert text.startswith(USER_RULES) and text == USER_RULES + dw.render_block(_rules("probe"))
