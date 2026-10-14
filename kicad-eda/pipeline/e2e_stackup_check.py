#!/usr/bin/env python3
"""Stackup e2e check (ADR D7, wave 2a; D7-29a): a board the gated harness built from a
netlist with a declared stackup and controlled-impedance nets, judged from the files
the run left in its folder. A run_all.json post_check ("python": "venv"): the dev venv,
no KiCad, stdin closed, file parsing only.

Usage: <venv-python> pipeline/e2e_stackup_check.py <dir> [--expect <example name>]

<dir> holds netlist.json and the run's outputs at the netlist stem (netlist.kicad_pcb,
netlist.kicad_dru, netlist_drc.json, netlist.stackup.json, netlist_pipeline_report.json,
netlist_gerbers/ and the fab zip the report names). The example's expectations
(EXPECTATIONS: copper count, job-file rows, thickness, IPC-2581, the G1 ST codes, the
ADR D7.14 e2e FD reference widths) are picked by the netlist's "name", or --expect for a
renamed copy; an unknown name runs the generic checks only. Checks:
  gates          report PASS, every gate G1..G5 passed
  g1_codes       the expected ST-W codes among the G1 lint warnings (ST-W3 / ST-W10 /
                 ST-W5 + ST-W6)
  stackup        the artifact's stackup is stackup_model.resolve(netlist) (the roles); the
                 board text enables exactly its copper with the KiCad layer types of the
                 roles, (general (thickness)) is the computed sum and the spliced block
                 equals stackup_kicad.emit (stackup_kicad.compare); the copper count
  rules          report['dru'] compiled (the canary fired), the managed block is in
                 <stem>.kicad_dru with the canary rule
  drc            report['drc'] 0 errors / 0 unconnected, and <stem>_drc.json agrees
  r8             no foreign-net track or arc on a plane-role layer (board text), and the
                 G4 role checks judged the board with no issue
  widths         every track / arc of an impedance net (a diff partner included) on the
                 report['dsn_policy'] routed_layers has the artifact's width +/- WIDTH_TOL_MM;
                 each net has at least one such track; none lies on another layer
  fd_references  the artifact widths vs the ADR D7.14 e2e FD widths, within FD_TOL_PCT
  gbrjob         fab_stackup.check_gbrjob: one Thickness row per copper, ply and mask (the
                 expected count), BoardThickness (the expected value), ImpedanceControlled,
                 inner copper numbered by position (L2..)
  ipc2581        <stem>_ipc2581.xml present iff fab_stackup.ipc2581_needed (and iff the
                 expectation says so): an XML file naming IPC-2581
  csv, notes     <stem>_stackup.csv rows for every copper layer and the total; the fab
                 notes name the rationale and every impedance net
  zip            the job file, CSV, notes and IPC-2581 are in the fab zip

Prints ONE JSON line {ok, dir, example, checks, problems}. Exit 0 when every check
passes, 1 when one fails, 2 on a usage error or when nothing can be checked (no netlist,
no report, no artifact, no board).
"""
from __future__ import annotations

import csv
import json
import sys
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple, TextIO

SERVER = Path(__file__).resolve().parents[1] / "server"
for _p in (SERVER / "tools", SERVER):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import board_text_copper as btc  # noqa: E402
import drc_guard as dg  # noqa: E402
import dru_writer as dw  # noqa: E402
import fab_stackup as fs  # noqa: E402
import physics_resolve as pr  # noqa: E402
import stackup_kicad as sk  # noqa: E402
import stackup_model as sm  # noqa: E402

USAGE = "usage: e2e_stackup_check.py <dir> [--expect <example name>]"
EXPECT_FLAG = "--expect"
EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
NETLIST = "netlist.json"
REPORT_SUFFIX = "_pipeline_report.json"
DRC_SUFFIX = dg.REPORT_SUFFIX
GERBER_DIR_SUFFIX = "_gerbers"
WIDTH_TOL_MM = 0.001
FD_TOL_PCT = 2.0
THICKNESS_TOL_MM = 0.001
GATES = ("G1", "G2", "G3", "G4", "G5")
TRACK_KINDS = frozenset({"track", "arc"})
PLANE_ROLE = "plane"
ST_PREFIX = "ST-"
IPC_MARK = "IPC-2581"
CSV_TOTAL = "total"
WIDTHS_SHOWN = 8


class Expectation(NamedTuple):
    """What one example's run must show (ADR D7.2.2 builds, D7.12 outputs, D7.14 widths)."""
    copper: int
    rows: int                          # job-file Thickness rows: copper + plies + 2 masks
    thickness: float                   # computed board thickness (mm)
    ipc2581: bool
    st_codes: tuple[str, ...]          # G1 warning codes the design must raise
    fd_refs: dict[tuple[str, str], float]   # (net, layer) -> FD width (mm), JLC/LPI mask


# ADR D7.14 e2e references (fine-grid FD, mask on): 2L FR-4 1.51 / Dk 4.5 CPWG gap 0.2 ->
# 0.8664; RO4350B 0.254 / Dk 3.66 microstrip -> 0.4942, CPWG gap 0.2 -> 0.3975. The 6L
# JLC06161H-2116 lines have no e2e reference (closed form / FD only).
_TWO_L = {(n, "F.Cu"): 0.8664 for n in ("RF_IN", "RF_MID", "RF_OUT")}
_HYBRID = {("RF_IN", "F.Cu"): 0.4942, ("AMP_IN", "F.Cu"): 0.4942,
           ("AMP_OUT", "F.Cu"): 0.3975, ("RF_OUT", "F.Cu"): 0.3975}
EXPECTATIONS: dict[str, Expectation] = {
    "stackup_2l_mixed": Expectation(2, 5, 1.641, False, ("ST-W3",), _TWO_L),
    "six_layer_mcu": Expectation(6, 16, 1.6554, True, ("ST-W10",), {}),
    "stackup_hybrid_4l": Expectation(4, 10, 1.3685, True, ("ST-W5", "ST-W6"), _HYBRID),
}


class UsageError(ValueError):
    """Bad command-line arguments."""


class CheckError(RuntimeError):
    """Nothing can be checked: the reason goes in the JSON's error."""


class Args(NamedTuple):
    dir: str
    expect: str | None = None


def parse_args(argv: Sequence[str]) -> Args:
    """<dir> [--expect NAME | --expect=NAME], the flag anywhere."""
    rest: list[str] = []
    expect, i = None, 0
    while i < len(argv):
        arg = argv[i]
        if arg == EXPECT_FLAG or arg.startswith(EXPECT_FLAG + "="):
            if arg == EXPECT_FLAG:
                if i + 1 >= len(argv) or argv[i + 1].startswith("-"):
                    raise UsageError(f"{EXPECT_FLAG} needs one of {', '.join(EXPECTATIONS)}")
                expect, i = argv[i + 1], i + 2
            else:
                expect, i = arg[len(EXPECT_FLAG) + 1:], i + 1
            if expect not in EXPECTATIONS:
                raise UsageError(f"{EXPECT_FLAG} must be one of {', '.join(EXPECTATIONS)}, "
                                 f"got {expect!r}")
            continue
        if arg.startswith("--"):
            raise UsageError(f"unknown option {arg}")
        rest, i = [*rest, arg], i + 1
    if len(rest) != 1:
        raise UsageError(f"expected one <dir>, got {len(rest)} argument(s)")
    return Args(rest[0], expect)


def expectation_for(design: Mapping[str, Any], expect: str | None) -> Expectation | None:
    """The example's expectations: --expect, else the netlist name; None when unknown."""
    name = expect if expect is not None else design.get("name")
    return EXPECTATIONS.get(name) if isinstance(name, str) else None


# --- gates and G1 (pure) --------------------------------------------------------------------
def _gates(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(g.get("gate")): g for g in report.get("gates") or ()
            if isinstance(g, Mapping)}


def gate_problems(report: Mapping[str, Any]) -> list[str]:
    """The run passed every gate G1..G5 and says PASS."""
    gates = _gates(report)
    out = []
    for name in GATES:
        g = gates.get(name)
        if g is None:
            out.append(f"the run never reached {name} (no {name} verdict in the report)")
        elif not g.get("passed"):
            first = next(iter(g.get("blockers") or ()), "no blocker recorded")
            out.append(f"{name} failed: {first}")
    if report.get("PASS") is not True:
        out.append("the report's PASS is not true")
    return out


def _st_codes(warnings: Iterable[Any]) -> list[str]:
    seen = {}
    for w in warnings:
        head = str(w).split(" ", 1)[0]
        if head.startswith(ST_PREFIX):
            seen.setdefault(head, None)
    return list(seen)


def lint_code_problems(report: Mapping[str, Any], codes: Sequence[str]) -> list[str]:
    """Each expected ST code heads at least one G1 lint warning."""
    lint = report.get("lint")
    warnings = lint.get("warnings") if isinstance(lint, Mapping) else None
    found = _st_codes(warnings if isinstance(warnings, list) else [])
    return [f"G1 warnings carry no {code} (ADR D7.2.7); the ST codes seen: "
            f"{', '.join(found) or 'none'}" for code in codes if code not in found]


# --- stackup, rules, DRC (pure) ------------------------------------------------------------------
def _resolved(design: Mapping[str, Any]) -> tuple[sm.ResolvedStackup | None, list[str]]:
    try:
        rs, issues = sm.resolve(design)
    except (TypeError, ValueError) as exc:
        return None, [f"the netlist stackup does not resolve: {exc}"]
    if rs is None:
        errors = [f"{i.id} {i.message}" for i in issues if i.severity == "error"]
        return None, ["the netlist stackup does not resolve: " + ("; ".join(errors) or "no "
                                                                   "stackup")]
    return rs, []


def stackup_problems(design: Mapping[str, Any], artifact: Mapping[str, Any],
                     board_text: str) -> list[str]:
    """The artifact carries the netlist's resolved stackup and the board carries the
    artifact's (copper, types = roles, thickness, the spliced block)."""
    rs, problems = _resolved(design)
    if rs is None:
        return problems
    want = json.loads(json.dumps(rs.to_dict()))
    got = artifact.get("stackup")
    if got != want:
        have = dict(got) if isinstance(got, Mapping) else {}
        keys = sorted(k for k in set(want) | set(have) if have.get(k) != want.get(k))
        problems.append("the artifact's stackup differs from stackup_model.resolve(netlist) at "
                        f"{', '.join(keys) or 'its shape'}")
    try:
        problems += sk.compare(rs, board_text)
    except ValueError as exc:
        problems.append(f"the board text cannot be read for its stackup: {exc}")
    return problems


def rules_problems(report: Mapping[str, Any], dru_text: str | None) -> list[str]:
    """The managed rules compiled (canary) and are in the board's rules file."""
    dru = report.get("dru")
    out = []
    if not isinstance(dru, Mapping) or dru.get("ok") is not True:
        why = dru.get("reason") if isinstance(dru, Mapping) else "no report['dru']"
        out.append(f"the managed rules did not compile (R23): {why or 'no reason given'}")
    elif not dru.get("fired"):
        out.append("the canary rule did not fire on the canary board: the rules file was "
                   "dropped silently (R23)")
    if dru_text is None:
        return [*out, "no <stem>.kicad_dru beside the board"]
    if dw.BEGIN_MARKER not in dru_text or dw.END_MARKER not in dru_text:
        out.append("the rules file has no managed block (dru_writer markers)")
    if dg.CANARY_NET not in dru_text:
        out.append("the rules file has no canary rule")
    return out


def _count(doc: Mapping[str, Any], key: str) -> int | None:
    items = doc.get(key)
    return len(items) if isinstance(items, list) else None


def drc_problems(report: Mapping[str, Any], drc_doc: Mapping[str, Any] | None) -> list[str]:
    """DRC 0 / 0 in the report and in the kicad-cli report on disk."""
    drc = report.get("drc") if isinstance(report.get("drc"), Mapping) else {}
    out = [f"report['drc'] {key} is {drc.get(key)!r}, not 0" for key in ("errors", "unconnected")
           if drc.get(key) != 0]
    if drc_doc is None:
        return [*out, f"no <stem>{DRC_SUFFIX} beside the board"]
    for key in ("violations", "unconnected_items"):
        n = _count(drc_doc, key)
        if n != 0:
            out.append(f"the DRC report on disk lists {n if n is not None else 'no'} "
                       f"{key.replace('_', ' ')}")
    return out


# --- copper: R8 and the line widths (pure) ------------------------------------------------------
def roles_of(artifact: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """{layer: {role, net}} of the artifact's stackup copper."""
    copper = (artifact.get("stackup") or {}).get("copper") or ()
    return {c["name"]: {"role": c.get("role"), "net": c.get("net")} for c in copper
            if isinstance(c, Mapping) and c.get("name")}


def _tracks(items: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [t for t in items if t.get("kind") in TRACK_KINDS]


def foreign_plane_tracks(items: Iterable[Mapping[str, Any]],
                         roles: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    """{plane layer: tracks / arcs of another net on it} (R8)."""
    planes = {layer: r.get("net") for layer, r in roles.items() if r.get("role") == PLANE_ROLE}
    counts = dict.fromkeys(planes, 0)
    for t in _tracks(items):
        layer = t.get("layer")
        if layer in planes and t.get("net") != planes[layer]:
            counts[layer] += 1
    return counts


def r8_problems(report: Mapping[str, Any], items: Iterable[Mapping[str, Any]],
                roles: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """No foreign copper on plane layers (board text) and the G4 role checks agree."""
    out = []
    for layer, n in foreign_plane_tracks(items, roles).items():
        if n:
            nets = sorted({str(t.get("net")) for t in _tracks(items) if t.get("layer") == layer
                           and t.get("net") != roles[layer].get("net")})
            out.append(f"R8: {n} track(s) of {', '.join(nets)} on the plane layer {layer} "
                       f"({roles[layer].get('net')})")
    lr = report.get("layer_roles")
    if not isinstance(lr, Mapping) or lr.get("judged") is not True:
        why = lr.get("reason") or lr.get("skipped") if isinstance(lr, Mapping) else None
        out.append(f"the G4 layer-role checks did not judge the board: {why or 'not run'}")
    elif lr.get("issues"):
        out.append(f"the G4 layer-role checks found issues: {'; '.join(map(str, lr['issues']))}")
    return out


def _partners(design: Mapping[str, Any]) -> dict[str, str]:
    specs = design.get("net_specs")
    out: dict[str, str] = {}
    for net, spec in (specs.items() if isinstance(specs, Mapping) else ()):
        partner = spec.get("diff_pair") if isinstance(spec, Mapping) else None
        if isinstance(partner, str) and partner and partner != net:
            out.setdefault(net, partner)
            out.setdefault(partner, net)
    return out


def line_widths(design: Mapping[str, Any], lines: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    """{net: {layer: artifact width}} of every impedance line, a diff partner without a
    record of its own taking its partner's widths."""
    out: dict[str, dict[str, float]] = {}
    for net, rec in lines.items():
        layers = rec.get("layers") if isinstance(rec, Mapping) else None
        if isinstance(layers, Mapping):
            out[net] = {layer: float(line["width"]) for layer, line in layers.items()
                        if isinstance(line, Mapping)
                        and isinstance(line.get("width"), (int, float))}
    for net, partner in _partners(design).items():
        if net in out and partner not in out:
            out[partner] = dict(out[net])
    return dict(sorted(out.items()))


def _by_net_layer(items: Iterable[Mapping[str, Any]], nets: Iterable[str]
                  ) -> dict[str, dict[str, list[float]]]:
    wanted = set(nets)
    out: dict[str, dict[str, list[float]]] = {n: {} for n in wanted}
    for t in _tracks(items):
        if t.get("net") in wanted:
            out[t["net"]].setdefault(str(t.get("layer")), []).append(float(t.get("width", 0.0)))
    return out


def width_problems(items: Iterable[Mapping[str, Any]], widths: Mapping[str, Mapping[str, float]],
                   routed: Mapping[str, Any]) -> list[str]:
    """Every track of an impedance net lies on its routed layers at the artifact width."""
    found = _by_net_layer(items, widths)
    out = []
    for net, per_layer in widths.items():
        layers = routed.get(net)
        if not isinstance(layers, list) or not layers:
            out.append(f"{net}: report['dsn_policy'] routed_layers names no layer for it")
            continue
        for layer, got in sorted(found[net].items()):
            if layer not in layers:
                out.append(f"{net}: {len(got)} track(s) on {layer}, outside its routed layers "
                           f"{layers} (R7 / the DSN layer policy)")
                continue
            want = per_layer.get(layer)
            off = sorted({w for w in got if want is None or abs(w - want) > WIDTH_TOL_MM + 1e-9})
            if off:
                out.append(f"{net} on {layer}: track width(s) {off[:WIDTHS_SHOWN]} mm, the "
                           f"artifact solved {want} mm (+/- {WIDTH_TOL_MM:g})")
        if not any(layer in layers for layer in found[net]):
            out.append(f"{net}: no track on its routed layers {layers}")
    return out


def width_summary(items: Iterable[Mapping[str, Any]], widths: Mapping[str, Mapping[str, float]],
                  routed: Mapping[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """{net: {routed layer: {tracks, artifact_mm, widths_mm}}} for the JSON."""
    found = _by_net_layer(items, widths)
    return {net: {layer: {"tracks": len(found[net].get(layer, [])),
                          "artifact_mm": per_layer.get(layer),
                          "widths_mm": sorted(set(found[net].get(layer, [])))}
                  for layer in (routed.get(net) or []) if isinstance(layer, str)}
            for net, per_layer in widths.items()}


def fd_problems(lines: Mapping[str, Any], refs: Mapping[tuple[str, str], float]) -> list[str]:
    """Each referenced (net, layer) width is within FD_TOL_PCT of its ADR D7.14 FD width."""
    out = []
    for (net, layer), want in sorted(refs.items()):
        rec = lines.get(net)
        line = (rec.get("layers") or {}).get(layer) if isinstance(rec, Mapping) else None
        got = line.get("width") if isinstance(line, Mapping) else None
        if not isinstance(got, (int, float)):
            out.append(f"{net} on {layer}: no line in the artifact (FD reference {want} mm)")
            continue
        pct = abs(got - want) / want * 100.0
        if pct > FD_TOL_PCT + 1e-9:
            out.append(f"{net} on {layer}: artifact width {got} mm is {pct:.1f} % from the ADR "
                       f"D7.14 FD width {want} mm (tolerance {FD_TOL_PCT:g} %, status "
                       f"{rec.get('status') if isinstance(rec, Mapping) else None})")
    return out


# --- fab outputs ------------------------------------------------------------------------------
def gbrjob_problems(job: Path, artifact: Mapping[str, Any], exp: Expectation | None) -> list[str]:
    """fab_stackup.check_gbrjob plus the expected row count and thickness (R24)."""
    if not job.is_file():
        return [f"no job file {job.name}"]
    try:
        check = fs.check_gbrjob(job, artifact)
        text = json.loads(job.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return [f"{job.name} cannot be checked: {exc}"]
    out = [f"{job.name}: {p}" for p in check["problems"]]
    if exp is None:
        return out
    if check["rows"] != exp.rows:
        out.append(f"{job.name}: {check['rows']} thickness rows, the example needs {exp.rows}")
    specs = text.get("GeneralSpecs") if isinstance(text.get("GeneralSpecs"), Mapping) else {}
    thickness = specs.get("BoardThickness")
    if not isinstance(thickness, (int, float)) or abs(thickness - exp.thickness) > THICKNESS_TOL_MM:
        out.append(f"{job.name}: BoardThickness {thickness!r} mm, the example needs "
                   f"{exp.thickness} mm (+/- {THICKNESS_TOL_MM:g})")
    return out


def ipc2581_problems(path: Path, artifact: Mapping[str, Any], exp: Expectation | None) -> list[str]:
    """IPC-2581 present iff needed (fab_stackup rule and the example's expectation)."""
    try:
        needed = fs.ipc2581_needed(artifact)
    except (ValueError, TypeError, KeyError) as exc:
        return [f"the IPC-2581 rule could not be evaluated: {exc}"]
    out = [] if exp is None or exp.ipc2581 is needed else [
        f"fab_stackup says IPC-2581 is {'' if needed else 'not '}needed, the example expects "
        f"{'one' if exp.ipc2581 else 'none'}"]
    if not needed:
        return [*out, f"{path.name} was exported although the build does not need it"] \
            if path.is_file() else out
    if not path.is_file():
        return [*out, f"no {path.name} (needed: {'; '.join(fs.ipc2581_reasons(artifact))})"]
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError as exc:
        return [*out, f"{path.name} unreadable: {exc}"]
    if not head.lstrip().startswith("<?xml") or IPC_MARK not in head:
        out.append(f"{path.name} is not an {IPC_MARK} XML document")
    return out


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def csv_problems(path: Path, artifact: Mapping[str, Any]) -> list[str]:
    """The stackup CSV lists every copper layer and the computed total."""
    if not path.is_file():
        return [f"no {path.name}"]
    try:
        rows = _csv_rows(path)
    except (OSError, csv.Error) as exc:
        return [f"{path.name} unreadable: {exc}"]
    st = artifact.get("stackup") or {}
    layers = {r.get("layer") for r in rows}
    out = [f"{path.name}: no row for {name}" for name in roles_of(artifact) if name not in layers]
    total = next((r for r in rows if r.get("type") == CSV_TOTAL), None)
    want = st.get("board_thickness_computed")
    try:
        got = float(total["thickness_mm"]) if total else None
    except (KeyError, TypeError, ValueError):
        got = None
    if got is None or not isinstance(want, (int, float)) or abs(got - want) > THICKNESS_TOL_MM:
        out.append(f"{path.name}: total thickness {got!r}, the stackup computes {want}")
    return out


def notes_problems(path: Path, artifact: Mapping[str, Any], design: Mapping[str, Any]) -> list[str]:
    """The fab notes carry the layer-count rationale and every impedance net."""
    if not path.is_file():
        return [f"no {path.name}"]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{path.name} unreadable: {exc}"]
    rationale = (artifact.get("stackup") or {}).get("rationale")
    out = [] if rationale and rationale in text else [
        f"{path.name} does not state the layer-count rationale {rationale!r}"]
    for net in line_widths(design, artifact.get("lines") or {}):
        if net not in text:
            out.append(f"{path.name} does not list the impedance net {net}")
    return out


def zip_problems(zip_path: Path | None, files: Sequence[Path]) -> list[str]:
    """Every file is in the fab zip (by name)."""
    if zip_path is None or not zip_path.is_file():
        return [f"no fab zip ({zip_path.name if zip_path else 'none named'})"]
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = set(z.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"the fab zip {zip_path.name} cannot be read: {exc}"]
    return [f"{p.name} is not in the fab zip {zip_path.name}" for p in files if p.name not in names]


# --- running it ---------------------------------------------------------------------------------
class RunFiles(NamedTuple):
    folder: Path
    stem: str
    design: dict
    report: dict
    artifact: dict
    board_text: str


def _read_json(path: Path, what: str, required: bool = True) -> dict | None:
    if not path.is_file():
        if required:
            raise CheckError(f"no {what} {path.name} in {path.parent}")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise CheckError(f"cannot read the {what} {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CheckError(f"the {what} {path} is not a JSON object")
    return data


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
    except OSError:
        return None


def load_run(folder: Path) -> RunFiles:
    """The run's netlist, report, artifact and board text (CheckError when one is missing)."""
    if not folder.is_dir():
        raise CheckError(f"{folder} is not a directory")
    netlist = folder / NETLIST
    stem = netlist.stem
    design = _read_json(netlist, "netlist")
    report = _read_json(folder / f"{stem}{REPORT_SUFFIX}", "pipeline report")
    try:
        artifact = pr.read_artifact(pr.artifact_path(netlist))
    except (OSError, ValueError) as exc:
        raise CheckError(f"no usable physical-design artifact: {exc}") from exc
    board = folder / f"{stem}{dw.BOARD_SUFFIX}"
    text = _read_text(board)
    if text is None:
        raise CheckError(f"no board {board.name} in {folder}")
    return RunFiles(folder, stem, design, report, artifact, text)


def _check(problems: list[str], **data: Any) -> dict:
    return {"ok": not problems, **data, "problems": problems}


def _copper_items(text: str) -> list[dict]:
    try:
        return list(btc.read_copper(text).items)
    except ValueError as exc:
        raise CheckError(f"the board's copper cannot be read: {exc}") from exc


def _fab_checks(run: RunFiles, exp: Expectation | None) -> dict[str, dict]:
    out = run.folder / f"{run.stem}{GERBER_DIR_SUFFIX}"
    job = out / f"{run.stem}{fs.GBRJOB_SUFFIX}"
    ipc = out / f"{run.stem}{fs.IPC2581_SUFFIX}"
    csv_path, notes = out / f"{run.stem}{fs.CSV_SUFFIX}", out / f"{run.stem}{fs.NOTES_SUFFIX}"
    fab = run.report.get("fab") if isinstance(run.report.get("fab"), Mapping) else {}
    zip_path = run.folder / str(fab["zip"]) if fab.get("zip") else None
    return {"gbrjob": _check(gbrjob_problems(job, run.artifact, exp), path=str(job)),
            "ipc2581": _check(ipc2581_problems(ipc, run.artifact, exp), path=str(ipc),
                              present=ipc.is_file()),
            "csv": _check(csv_problems(csv_path, run.artifact), path=str(csv_path)),
            "notes": _check(notes_problems(notes, run.artifact, run.design), path=str(notes)),
            "zip": _check(zip_problems(zip_path, [p for p in (job, csv_path, notes, ipc)
                                                  if p.is_file()]),
                          path=str(zip_path) if zip_path else None)}


def _copper_checks(run: RunFiles, exp: Expectation | None) -> dict[str, dict]:
    items = _copper_items(run.board_text)
    roles = roles_of(run.artifact)
    widths = line_widths(run.design, run.artifact.get("lines") or {})
    policy = run.report.get("dsn_policy")
    routed = policy.get("routed_layers") if isinstance(policy, Mapping) else None
    routed = routed if isinstance(routed, Mapping) else {}
    refs = exp.fd_refs if exp is not None else {}
    return {"r8": _check(r8_problems(run.report, items, roles),
                         foreign=foreign_plane_tracks(items, roles)),
            "widths": _check(width_problems(items, widths, routed),
                             lines=width_summary(items, widths, routed)),
            "fd_references": _check(fd_problems(run.artifact.get("lines") or {}, refs),
                                    compared=len(refs))}


def _copper_count_problems(run: RunFiles, exp: Expectation | None) -> list[str]:
    if exp is None:
        return []
    names = list(roles_of(run.artifact))
    return [] if len(names) == exp.copper else [
        f"the stackup has {len(names)} copper layers ({', '.join(names)}), the example needs "
        f"{exp.copper}"]


def run_checks(args: Args) -> dict:
    """Every check of the module doc for the run in args.dir (CheckError when none can run)."""
    run = load_run(Path(args.dir))
    exp = expectation_for(run.design, args.expect)
    name = args.expect or run.design.get("name")
    checks = {
        "gates": _check(gate_problems(run.report)),
        "g1_codes": _check(lint_code_problems(run.report, exp.st_codes if exp else ()),
                           expected=list(exp.st_codes) if exp else []),
        "stackup": _check(stackup_problems(run.design, run.artifact, run.board_text)
                          + _copper_count_problems(run, exp), copper=list(roles_of(run.artifact))),
        "rules": _check(rules_problems(run.report,
                                       _read_text(dw.dru_path_for(run.folder / f"{run.stem}"
                                                                  f"{dw.BOARD_SUFFIX}")))),
        "drc": _check(drc_problems(run.report, _read_json(run.folder / f"{run.stem}{DRC_SUFFIX}",
                                                          "DRC report", required=False))),
        **_copper_checks(run, exp), **_fab_checks(run, exp)}
    problems = [f"{k}: {p}" for k, c in checks.items() for p in c["problems"]]
    return {"ok": not problems, "dir": str(run.folder), "example": name,
            "expectation": exp is not None, "checks": checks, "problems": problems}


def _emit(result: Mapping, out: TextIO) -> None:
    print(json.dumps(result, default=str), file=out)


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    out = out if out is not None else sys.stdout
    try:
        args = parse_args(list(sys.argv[1:] if argv is None else argv))
    except UsageError as exc:
        print(f"e2e_stackup_check: error: {exc}\n{USAGE}", file=sys.stderr)
        _emit({"ok": False, "error": f"usage: {exc}"}, out)
        return EXIT_USAGE
    try:
        result = run_checks(args)
    except CheckError as exc:
        _emit({"ok": False, "dir": args.dir, "error": str(exc)}, out)
        return EXIT_USAGE
    _emit(result, out)
    return EXIT_OK if result["ok"] else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
