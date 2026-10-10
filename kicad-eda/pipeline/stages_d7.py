"""The D7 harness stages (ADR D7, wave 2a; D7-24a): stackups, layer roles and controlled
impedance inside the gated run. pipeline/rp_feature_d7.py is the only caller: it wires these
functions to rp_hooks hooks and writes the report keys; nothing here reads or writes the
RunContext or the report.

  G1     apply_layer_default: a napkin netlist ("mode": "napkin") that declares no layer
         count gets the ADR D7.11 default (layer_policy.napkin_default) written into the
         netlist, atomically; the harness's default napkin behaviour alone never rewrites
         the count (not_applied_reason says why nothing was written)
  G2     resolve_physics: physics_resolve.write_artifact for the board the run builds
         (<netlist stem>.stackup.json + the managed <board stem>.kicad_dru, before G3);
         physics_summary / physics_problems. compile_rules: the canary (drc_guard,
         R23) on the board's whole rules file with every kicad-cli whose DRC will read it
  hash   physics_data_version: the data version of the materials, fabs and tline data
  veto   role_rule_reasons / veto: impedance-controlled nets, R27 reference transitions,
         R28 dual-stripline runs (role rules the DRU cannot express) and RF chains need
         the SWIG checks of a KiCad 9/10 board (ADR D7.19)
  G4     judge_roles: swig_layer_role_checks.py (R8 blockers, R27/R28 warnings) once per
         board content; a KiCad 11 board (tc.swig_readable) gets the K11_BLOCKER when the
         design has such rules, else the K11_NOTE warning; dsn_policy_report: the DSN
         layer policy the artifact implies (dsn_layers.narrow) and what the routed
         <pcb stem>.dsn really carries
  EMC    emc_args: emc_stitch.py --stackup=<artifact>
  G5     fab_stackup_check: the job file, IPC-2581, stackup CSV and fab notes that
         export_fab_package wrote (ADR D7.12, R24) checked on disk and in the zip;
         fab_blockers turns each problem into a G5 blocker
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

import rp_paths  # noqa: F401  first: puts server/ and server/tools/ on sys.path
import drc_guard
import dru_writer
import dsn_layers as dl
import dsn_sexpr as ds
import fab_stackup as fs
import layer_policy as lp
import physics_resolve as pr
import toolchain as tc

FD_MODE = "auto"                  # physics_resolve fd mode of the harness (the unit tests: off)
ROLE_CHECK_SCRIPT = rp_paths.SERVER / "tools" / "swig_layer_role_checks.py"
ROLE_CHECK_TIMEOUT_S = 300
OUTPUT_TAIL = 300                 # chars of a child's output kept when it printed no result
GERBER_DIR_SUFFIX = "_gerbers"    # board_fab.export's default out_dir: <pcb without suffix>_gerbers
DSN_SUFFIX = ".dsn"               # board_autoroute writes <pcb without suffix>.dsn
STITCH_FLAG = "--stackup"         # = emc_stitch.STACKUP_FLAG
NAMES_SHOWN = 6
VETO_REASON = ("D7 layer-role / impedance checks need a KiCad 9/10 board; the KiCad 11 agent "
               "path is wave 3 (D7-26)")
K11_BLOCKER = "D7 layer-role checks cannot run on a KiCad 11 board"
K11_NOTE = "R8 was judged by the managed DRU only"
NOT_JUDGED = "D7 layer-role checks not judged"
NO_ARTIFACT = ("no physical-design artifact (<netlist stem>.stackup.json): the D7 physics "
               "stage did not write one")
NOTHING_TO_CHECK = "no plane-role layer and no impedance line: nothing to check"
PHYSICS_HALT = "physical design (ADR D7.4)"
RULES_HALT = "managed design rules (ADR D7.8 R23)"
FAB_BLOCKER = "fab stackup (ADR D7.12, R24)"
NOT_NAPKIN = ('the netlist does not set "mode": "napkin" (the harness default napkin behaviour '
              "never rewrites the layer count): without a declaration the 4-layer ADR D7.2.4 "
              "default is used and G1 warns ST-W12")
DECLARED = ("the netlist declares its layer count (copper_layers or a stackup): the napkin "
            "default (ADR D7.11) does not apply")
SUMMARY_KEYS = ("ok", "dru_path", "changed", "decisions_written", "decisions", "fd", "previous",
                "design_digest", "data_version", "warnings", "errors")
CANARY_KEYS = ("ok", "rc", "crash", "fired", "canary_added", "types", "reason")
ROLE_KEYS = ("ok", "issues", "warnings", "metrics", "stackup", "board_copper", "stackup_copper",
             "pcbnew")
NO_POLICY = ("dsn_policy: the routed DSN {name} carries no layer policy (no kicad_eda_ class, no "
             "use_layer): Freerouting could route signals across the plane layers; R8 is still "
             "judged by the role checks and the managed DRU")


def _names(items: Sequence[str]) -> str:
    shown = ", ".join(items[:NAMES_SHOWN])
    more = len(items) - NAMES_SHOWN
    return f"{shown} and {more} more" if more > 0 else shown


def _sha256(path: str | os.PathLike[str]) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def write_json_atomic(path: Path, doc: Any) -> None:
    """Replace `path` with `doc` as JSON (indent 2, as the G2 remediation writes netlists):
    a temp file in the same folder, then os.replace; nothing is left behind on a failure."""
    handle, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as fh:
            fh.write(json.dumps(doc, indent=2).encode("utf-8"))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# --- G1: the napkin layer default (ADR D7.11) -----------------------------------------------
def apply_layer_default(netlist: str, design: Any) -> tuple[dict, lp.NapkinDefault] | None:
    """The D7.11 default written into `netlist`: (the new netlist object, the NapkinDefault),
    or None when it does not apply (not a napkin netlist, or a count is declared). Never
    mutates `design`."""
    if not isinstance(design, Mapping):
        return None
    nd = lp.napkin_default(design)
    if nd is None:
        return None
    new = nd.apply(design)
    write_json_atomic(Path(netlist), new)
    return new, nd


def not_applied_reason(design: Any) -> str:
    """Why apply_layer_default wrote nothing."""
    if not isinstance(design, Mapping) or not lp.is_napkin(design):
        return NOT_NAPKIN
    return DECLARED


def remediation_note(nd: lp.NapkinDefault) -> str:
    """The G1 remediation-log entry of the napkin default."""
    return (f"napkin layer default (ADR D7.11): copper_layers {nd.copper_layers} and "
            f"stackup.preset {nd.preset} (origin layer_policy, with its rationale) written into "
            "the netlist")


def policy_record(nd: lp.NapkinDefault, decision: Mapping[str, Any] | None) -> dict[str, Any]:
    """report['layer_policy'] of an applied default (`decision`: the entry written, or None
    when the log could not take it)."""
    return {"applied": True, "copper_layers": nd.copper_layers, "preset": nd.preset,
            "rationale": nd.rationale, "floors": [f.to_dict() for f in nd.floors],
            "decision": decision.get("id") if decision else None}


# --- G2: the physical design (ADR D7.4) and the canary (R23) ---------------------------------
def resolve_physics(netlist: str, design: Any, pcb: str) -> dict[str, Any]:
    """physics_resolve.write_artifact for the board the run builds (the artifact, the managed
    <pcb stem>.kicad_dru, new decision drafts). Raises OSError / ValueError / TypeError for an
    unusable netlist or board path."""
    return pr.write_artifact(netlist, design, board_path=pcb, fd=FD_MODE)


def load_artifact(path: str | os.PathLike[str] | None) -> dict[str, Any] | None:
    """A physical@1 artifact, or None when there is no readable one at `path`."""
    if not path:
        return None
    try:
        return pr.read_artifact(path)
    except (OSError, ValueError):
        return None


def physics_summary(written: Mapping[str, Any], artifact: Mapping[str, Any] | None) -> dict:
    """report['physics']: the write_artifact result (artifact path, FD report, decisions,
    warnings, errors) plus the artifact's stackup, lines and rules sections."""
    out = {"artifact": written.get("path"), **{k: written.get(k) for k in SUMMARY_KEYS}}
    if artifact is None:
        return out
    return {**out, "stackup": artifact.get("stackup"), "lines": artifact.get("lines") or {},
            "rules": artifact.get("dru")}


def physics_failure(exc: BaseException) -> dict[str, Any]:
    """report['physics'] when write_artifact raised."""
    return {"ok": False, "artifact": None, "errors": [f"{type(exc).__name__}: {exc}"],
            "warnings": []}


def physics_problems(written: Mapping[str, Any]) -> list[str]:
    """The errors that stop the run at G2 ([] when the artifact is usable)."""
    errors = [str(e) for e in written.get("errors") or []]
    if errors or written.get("ok"):
        return errors
    return ["physics_resolve.write_artifact reported a failure without naming an error"]


def rule_clis(pcb: str) -> list[str | None]:
    """The kicad-cli(s) whose DRC reads the board's rules: the pcbnew install's (the harness
    builds and checks with it), plus the one of the KiCad that wrote an existing board when
    that is another (a resumed KiCad 11 board)."""
    first = tc.board_cli()
    own = tc.board_cli(pcb) if Path(pcb).is_file() else None
    return [first, own] if own and own != first else [first]


def _canary(text: str, cli: str | None) -> dict[str, Any]:
    res = drc_guard.rules_compile(text, cli)
    return {**{k: res[k] for k in CANARY_KEYS if k in res}, "kicad_cli": cli}


def compile_rules(pcb: str) -> dict[str, Any]:
    """The canary on the board's whole <pcb stem>.kicad_dru (the managed block and any user
    rules: KiCad compiles the file as one) with each rule_clis cli: {path, ok, kicad_cli, rc,
    crash, fired, canary_added, types[, reason][, others: the other clis' results]}."""
    path = dru_writer.dru_path_for(pcb)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"path": str(path), "ok": False,
                "reason": f"the rules file {path} cannot be read: {exc}"}
    first, *others = [_canary(text, cli) for cli in rule_clis(pcb)]
    failed = [r for r in (first, *others) if not r.get("ok")]
    out = {"path": str(path), **first, "ok": not failed, **({"others": others} if others else {})}
    if failed:
        out["reason"] = "; ".join(f"{r['kicad_cli']}: {r.get('reason') or 'did not compile'}"
                                  for r in failed)
    return out


def rules_record(compiled: Mapping[str, Any], artifact: Mapping[str, Any] | None) -> dict:
    """report['dru']: the canary result plus the artifact's rule count, families and hash."""
    dru = (artifact or {}).get("dru") or {}
    return {**{k: dru[k] for k in ("rules", "families", "sha256") if k in dru}, **compiled}


def physics_data_version() -> str:
    """The route-hash input of the D7 data (materials, fab presets, tline fallback)."""
    return pr.data_version()


# --- the handoff veto (ADR D7.19) -------------------------------------------------------------
def _ref_sets(rec: Any) -> set[frozenset[str]]:
    layers = rec.get("layers") if isinstance(rec, Mapping) else None
    return {frozenset((line.get("ref") or {}).get("nets") or ())
            for line in (layers or {}).values() if isinstance(line, Mapping)}


def _dual(lines: Mapping[str, Any]) -> list[str]:
    return [f"{net} on {layer} (over {', '.join(line['dielectric_layers'])})"
            for net, rec in sorted(lines.items()) if isinstance(rec, Mapping)
            for layer, line in sorted((rec.get("layers") or {}).items())
            if isinstance(line, Mapping) and line.get("dielectric_layers")]


def _targets(design: Mapping[str, Any]) -> list[str]:
    specs = design.get("net_specs")
    return sorted(n for n, s in (specs.items() if isinstance(specs, Mapping) else ())
                  if isinstance(s, Mapping)
                  and any(s.get(k) is not None for k in ("impedance", "diff", "freq_hz")))


def _rf_items(design: Mapping[str, Any]) -> int:
    rf = design.get("rf")
    if not isinstance(rf, Mapping):
        return 0
    return sum(len(rf[k]) for k in ("chains", "antennas") if isinstance(rf.get(k), list))


def _line_reasons(lines: Mapping[str, Any]) -> list[str]:
    out = [f"impedance-controlled nets {_names(sorted(lines))} (their checks are SWIG-only)"]
    r27 = [net for net, rec in sorted(lines.items()) if len(_ref_sets(rec)) > 1]
    if r27:
        out.append(f"R27 reference transitions on {_names(r27)} (allowed layers rest on "
                   "different reference nets)")
    dual = _dual(lines)
    if dual:
        out.append(f"R28 dual-stripline runs: {_names(dual)}")
    return out


def role_rule_reasons(artifact: Mapping[str, Any] | None, design: Any) -> list[str]:
    """Why the board needs the SWIG D7 checks of a KiCad 9/10 board: impedance-controlled
    nets, the rules the DRU cannot express (R27 reference transitions, R28 dual-stripline
    runs), RF chains / antennas ([] when it needs none). Without an artifact the design's
    impedance targets stand in for its lines."""
    design = design if isinstance(design, Mapping) else {}
    lines = (artifact or {}).get("lines")
    out: list[str] = []
    if isinstance(lines, Mapping) and lines:
        out = _line_reasons(lines)
    elif artifact is None and _targets(design):
        out = [f"impedance targets in net_specs ({_names(_targets(design))}) and no artifact "
               "saying how they resolve"]
    rf = _rf_items(design)
    return [*out, f"RF chains / antennas ({rf}): the RF checks are SWIG-only"] if rf else out


def veto(artifact: Mapping[str, Any] | None, design: Any) -> str | None:
    """The handoff_veto reason (VETO_REASON + why), or None when the agent may route it."""
    reasons = role_rule_reasons(artifact, design)
    return f"{VETO_REASON} ({'; '.join(reasons)})" if reasons else None


# --- G4: the layer-role checks (R8, R27, R28) ---------------------------------------------------
class Roles(NamedTuple):
    """One G4 judgement: report['layer_roles'], the G4 blockers and warnings, and the
    script results to keep ({board sha256: result})."""
    report: dict
    blockers: list[str]
    warnings: list[str]
    checked: dict


def role_check_skip(artifact: Mapping[str, Any]) -> str | None:
    """Why the SWIG role checks have nothing to judge on this design, or None."""
    copper = (artifact.get("stackup") or {}).get("copper") or []
    planes = any(isinstance(c, Mapping) and c.get("role") == "plane" for c in copper)
    return None if planes or artifact.get("lines") else NOTHING_TO_CHECK


def run_role_checks(pcb: str, netlist: str, artifact_path: str | None) -> dict[str, Any]:
    """swig_layer_role_checks.py under KiCad's Python: its result ({ok: True, issues,
    warnings, metrics, ...}) or {ok: False, error} when it could not judge the board."""
    kpy = tc.kicad_python()
    if not kpy:
        why = tc.kicad_python_error() or "no install has a bundled Python that imports pcbnew"
        return {"ok": False, "error": f"KiCad's pcbnew Python not found ({why})"}
    argv = [kpy, str(ROLE_CHECK_SCRIPT), pcb, netlist, *([artifact_path] if artifact_path else [])]
    try:
        r = tc.run(argv, timeout=ROLE_CHECK_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"{ROLE_CHECK_SCRIPT.name} did not run: "
                                      f"{type(exc).__name__}: {exc}"}
    found = tc.last_json(r.stdout or "")
    if found is None:
        tail = ((r.stdout or "") + (r.stderr or "")).strip()[-OUTPUT_TAIL:]
        return {"ok": False, "error": f"{ROLE_CHECK_SCRIPT.name} printed no result "
                                      f"(exit {r.returncode}): {tail}"}
    if not found.get("ok"):
        return {"ok": False, "error": str(found.get("error") or "no reason given")}
    return found


def k11_roles(name: str, why: str, artifact: Mapping[str, Any], design: Any) -> Roles:
    """A board pcbnew cannot load (a resumed KiCad 11 board): a blocker when the design has
    rules only the SWIG checks judge, else the note that the DRU alone judged R8."""
    reasons = role_rule_reasons(artifact, design)
    report = {"judged": False, "reason": why, "needs": reasons}
    if reasons:
        return Roles(report, [f"{K11_BLOCKER} ({name}): {'; '.join(reasons)}; route it with "
                              "Freerouting (--resume stalled) so they can be judged"], [], {})
    return Roles(report, [], [f"D7 layer-role checks: {K11_NOTE} ({name} is a KiCad 11 board "
                              "the SWIG checks cannot load; no R27 / R28 rule applies)"], {})


def judge_roles(pcb: str, netlist: str, artifact_path: str | None,
                artifact: Mapping[str, Any] | None, design: Any,
                cache: Mapping[str, dict]) -> Roles:
    """The G4 role judgement of the board as it is now (module doc); `cache` holds earlier
    script results by board sha256, so one board content is checked once."""
    name = Path(pcb).name
    if artifact is None:
        return Roles({"judged": False, "reason": NO_ARTIFACT},
                     [f"{NOT_JUDGED} on {name}: {NO_ARTIFACT}"], [], {})
    unreadable = tc.swig_readable(pcb)
    if unreadable is not None:
        return k11_roles(name, unreadable, artifact, design)
    skip = role_check_skip(artifact)
    if skip:
        return Roles({"judged": False, "skipped": skip}, [], [], {})
    key = _sha256(pcb)
    res = cache.get(key) if key else None
    res = res if res is not None else run_role_checks(pcb, netlist, artifact_path)
    if not res.get("ok"):
        why = res.get("error") or "no reason given"
        return Roles({"judged": False, "ok": False, "error": why},
                     [f"{NOT_JUDGED} on {name}: {why}"], [], {})
    report = {"judged": True, **{k: res.get(k) for k in ROLE_KEYS}, "board_sha256": key}
    return Roles(report, [str(i) for i in res.get("issues") or []],
                 [str(w) for w in res.get("warnings") or []], {key: res} if key else {})


def amend(verdict: dict, blockers: Sequence[str], warnings: Sequence[str]) -> dict | None:
    """A new verdict with the extra blockers and warnings (None when there are none)."""
    if not blockers and not warnings:
        return None
    return {**verdict, "passed": bool(verdict["passed"]) and not blockers,
            "blockers": [*verdict["blockers"], *blockers],
            "warnings": [*verdict["warnings"], *warnings]}


# --- G4: the DSN layer policy (ADR D7.9) --------------------------------------------------------
def dsn_path(pcb: str) -> Path:
    """The DSN board_autoroute exported (and post-processed) for `pcb`."""
    return Path(pcb).with_suffix(DSN_SUFFIX)


def expected_policy(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """What dsn_layers.postprocess makes of the artifact's dsn section: the Freerouting layers,
    layer rules and excluded layers of every impedance net (dsn_layers.narrow over the
    stackup's copper order). Raises dsn_layers.DsnLayersError for an invalid section."""
    pol = dl.load_policy(artifact["dsn"])
    copper = [c.get("name") for c in (artifact.get("stackup") or {}).get("copper") or []
              if isinstance(c, Mapping)]
    order = copper or list(pol.signal_layers)
    base = [x for x in order if x in pol.signal_layers and x not in pol.unused_layers]
    narrowed = {net: dl.narrow(order, base, pol.net_layers[net], dl.LAYER_RULE_VERIFIED)
                for net in sorted(pol.net_layers)}
    return {"source": "artifact", "variant": pol.plane_classes, "signal_layers": base,
            "plane_nets": {n: list(v) for n, v in pol.plane_nets.items()},
            "unused_layers": list(pol.unused_layers),
            "routed_layers": {n: list(r[0]) for n, r in narrowed.items()},
            "layer_rules": {n: dict(r[1]) for n, r in narrowed.items() if r[1]},
            "excluded_layers": {n: list(r[2]) for n, r in narrowed.items() if r[2]},
            "layer_rule_verified": dl.LAYER_RULE_VERIFIED}


def dsn_classes(text: str) -> dict[str, dict[str, Any]]:
    """{class name: {nets, use_layer (None: unrestricted)}} of a DSN, through dsn_sexpr."""
    q = ds.declared_quote(text)
    roots = ds.parse_nodes(text)
    network = ds.child(roots[0], "network") if roots else None
    out: dict[str, dict[str, Any]] = {}
    for node in ds.find_all(network, "class") if network is not None else []:
        toks = [x for x in node.items if isinstance(x, ds.Token)]
        if not toks:
            continue
        circuit = ds.child(node, "circuit")
        use = ds.child(circuit, "use_layer") if circuit is not None else None
        out[ds.unquote(toks[0].value, q)] = {
            "nets": [ds.unquote(t.value, q) for t in toks[1:]],
            "use_layer": None if use is None else [ds.unquote(t.value, q) for t in use.items
                                                   if isinstance(t, ds.Token)]}
    return out


def _variant(classes: Mapping[str, Mapping[str, Any]]) -> str | None:
    planes = [c["use_layer"] is not None for n, c in classes.items()
              if n.startswith(dl.PLANE_CLASS_PREFIX)]
    if not planes:
        return None
    return dl.RESTRICTED if all(planes) else dl.UNRESTRICTED if not any(planes) else "mixed"


def _mismatches(classes: Mapping[str, Mapping[str, Any]], expected: Mapping[str, Any]) -> list:
    out = []
    for net, want in expected["routed_layers"].items():
        got = next((c["use_layer"] for c in classes.values() if net in c["nets"]), want)
        if got != want:
            out.append(f"dsn_policy: the routed DSN restricts {net} to {got or 'every layer'} "
                       f"but its artifact routes it on {want}: the board was routed with "
                       "another layer policy (re-route it with PIPELINE_NO_CACHE=1)")
    return out


def routed_dsn(path: Path, expected: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """What the routed DSN carries ({path, found, applied, variant, classes}) and the G4
    warnings when it does not carry the artifact's policy."""
    if not path.is_file():
        return {"path": str(path), "found": False}, []
    try:
        classes = dsn_classes(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        return ({"path": str(path), "found": True, "error": str(exc)},
                [f"dsn_policy: the routed DSN {path.name} cannot be read ({exc})"])
    applied = any(n.startswith("kicad_eda_") or c["use_layer"] is not None
                  for n, c in classes.items())
    record = {"path": str(path), "found": True, "applied": applied,
              "variant": _variant(classes), "classes": len(classes)}
    if not applied:
        needed = expected["plane_nets"] or expected["routed_layers"]
        return record, [NO_POLICY.format(name=path.name)] if needed else []
    return record, _mismatches(classes, expected)


def dsn_policy_report(artifact: Mapping[str, Any] | None, artifact_path: str | None,
                      dsn: Path) -> tuple[dict[str, Any], list[str]]:
    """(report['dsn_policy'], G4 warnings): the policy the artifact implies and what the routed
    DSN `dsn` carries."""
    section = artifact.get("dsn") if isinstance(artifact, Mapping) else None
    if not isinstance(section, Mapping):
        return {"applied": False, "artifact": artifact_path,
                "reason": "no DSN layer policy (no artifact, or its stackup did not resolve)"}, []
    try:
        expected = expected_policy(artifact)
    except dl.DsnLayersError as exc:
        return ({"applied": False, "artifact": artifact_path, "reason": str(exc)},
                [f"dsn_policy: the artifact's DSN layer policy is invalid: {exc}"])
    seen, warnings = routed_dsn(Path(dsn), expected)
    return {**expected, "artifact": artifact_path, "dsn": seen}, warnings


# --- EMC and G5 -----------------------------------------------------------------------------
def emc_args(artifact_path: str | None) -> list[str]:
    """emc_stitch.py's --stackup=<artifact> (roles, single-sided guard), [] without one."""
    return [f"{STITCH_FLAG}={artifact_path}"] if artifact_path and Path(artifact_path).is_file() \
        else []


def board_artifact_path(pcb: str) -> Path:
    """<board stem>.stackup.json: what export_fab_package reads beside the board."""
    return Path(pcb).with_name(Path(pcb).stem + pr.ARTIFACT_SUFFIX)


def gerber_dir(pcb: str) -> Path:
    return Path(str(Path(pcb).with_suffix("")) + GERBER_DIR_SUFFIX)


def _job_check(job: Path, artifact: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    if not job.is_file():
        return {"path": None}, []
    try:
        check = fs.check_gbrjob(job, artifact)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"path": str(job)}, [f"{job.name} cannot be checked: {exc}"]
    return ({"path": str(job), "rows": check["rows"], "expected_rows": check["expected_rows"],
             "notes": check["notes"]}, [f"{job.name}: {p}" for p in check["problems"]])


def _zip_problems(zip_path: str | None, files: Sequence[Path]) -> list[str]:
    if not zip_path:
        return ["no fab zip to check: export_fab_package named none"]
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = set(z.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"the fab zip {Path(zip_path).name} cannot be read: {exc}"]
    return [f"{p.name} is not in the fab zip {Path(zip_path).name}" for p in files
            if p.name not in names]


def fab_stackup_check(pcb: str, artifact: Mapping[str, Any], zip_path: str | None) -> dict:
    """The fab outputs that carry the stackup, as export_fab_package left them in
    <pcb without suffix>_gerbers/ and the zip: {dir, gbrjob {path, rows, expected_rows,
    notes}, ipc2581 {path, reasons} | None, csv, notes, zip, problems}."""
    out, stem = gerber_dir(pcb), Path(pcb).stem
    try:
        reasons, problems = fs.ipc2581_reasons(artifact), []
    except (ValueError, TypeError, KeyError) as exc:
        reasons, problems = [], [f"the IPC-2581 rule could not be evaluated: {exc}"]
    files = {"gbrjob": out / f"{stem}{fs.GBRJOB_SUFFIX}", "csv": out / f"{stem}{fs.CSV_SUFFIX}",
             "notes": out / f"{stem}{fs.NOTES_SUFFIX}",
             **({"ipc2581": out / f"{stem}{fs.IPC2581_SUFFIX}"} if reasons else {})}
    missing = [f"{p.name} is missing from {out.name}/" for p in files.values() if not p.is_file()]
    job, job_problems = _job_check(files["gbrjob"], artifact)
    zipped = _zip_problems(zip_path, [p for p in files.values() if p.is_file()])
    return {"dir": str(out), "gbrjob": job,
            "ipc2581": {"path": str(files["ipc2581"]), "reasons": reasons} if reasons else None,
            "csv": str(files["csv"]), "notes": str(files["notes"]), "zip": zip_path,
            "problems": [*problems, *missing, *job_problems, *zipped]}


def fab_blockers(check: Mapping[str, Any]) -> list[str]:
    """Each fab-stackup problem as a G5 blocker."""
    return [f"{FAB_BLOCKER}: {p}" for p in check.get("problems") or []]
