"""The stackup side of the board tools (ADR D7.4 board-side lookup, D7.9, D7.12; D7-23a).

board_build, board_autoroute, board_fab and board_pipeline_tools only CALL in here (hook
lines); the logic lives here so the X-09 modules stay small. The artifact is physics_resolve's
physical-design file (schema kicad-eda/physical@1).

Lookup (artifact_for): an explicit ``stackup_json`` argument, else ``<netlist stem>.stackup.json``
beside the netlist (the build), else ``<board stem>.stackup.json`` beside the board (the tools
that receive only a board). Without one a board-only tool runs exactly as before and says so
(``"physics": {"artifact": None, "note"}``); export_fab_package keeps its result shape then.

* build_board_from_netlist: ensure_artifact before the SWIG child. A netlist that declares a
  stackup or copper_layers (or already has an artifact) gets it (re-)resolved when missing or
  stale (physics_resolve.write_artifact: the artifact, the managed ``<board stem>.kicad_dru``,
  new decision drafts); its errors refuse the build. A board in another folder or with another
  stem gets a byte-identical ``<board stem>.stackup.json`` mirror. builder_env hands the
  artifact to swig_build_board as KICAD_EDA_STACKUP (an inherited value is never passed on).
  A design that declares nothing and has no artifact keeps today's build exactly (no files,
  no env), with the note NOT_DECLARED (a board-stem mirror of an earlier build is removed).
* autoroute_board: route_policy -> apply_dsn_policy (dsn_layers.postprocess on the exported
  DSN, before the canonical order; report ``dsn_policy``; pad_layer_excluded or a policy that
  does not fit the DSN stops at step "dsn") -> ses_args (the artifact as swig_import_ses.py
  argv[3]) -> plane_fallback: when the restricted plane classes leave a pad of a plane net
  unconnected (the fresh DRC report), the pre-route board is restored and routed ONCE more with
  the unrestricted variant (T10); the route with fewer unconnected items is kept and
  ``dsn_policy.fallback`` {reason, unconnected_before, unconnected_after, ...} plus a G4
  ``decision_drafts`` entry record it.
* add_ground_stitching: stitch_args -> emc_stitch.py ``--stackup=<artifact>``.
* export_fab_package: fab_stackup_outputs before the zip (the job file's copper FileFunctions
  fixed from the Gerbers and checked against the stackup, ``<stem>_ipc2581.xml`` when
  fab_stackup.ipc2581_reasons gives one, ``<stem>_stackup.csv``, ``<stem>_fab_notes.md``);
  with_fab_stackup turns their problems into ok False.

server/tools modules are imported on first use (server/tools put on sys.path then), never at
import; toolchain is imported as a module, so tests that patch tc.run reach this code.
"""
from __future__ import annotations

import contextlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import toolchain as tc

SCHEMA = "kicad-eda/physical@1"            # = physics_resolve.SCHEMA (pinned by the tests)
ARTIFACT_SUFFIX = ".stackup.json"          # = physics_resolve.ARTIFACT_SUFFIX
NETLIST_SUFFIX = ".json"
ENV_VAR = "KICAD_EDA_STACKUP"              # = swig_stackup.ARTIFACT_ENV
DECLARED_KEYS = ("stackup", "copper_layers")
RESTRICTED, UNRESTRICTED = "restricted", "unrestricted"   # = dsn_layers.VARIANTS
PREROUTE_SUFFIX = ".preroute"              # the board before Freerouting (fallback restore)
FIRST_ROUTE_SUFFIX = ".route1"             # the restricted route while the fallback runs
STITCH_FLAG = "--stackup"                  # = emc_stitch.STACKUP_FLAG
IPC2581_TIMEOUT_S = 300
FALLBACK_SOURCE = "ADR D7.9: T10 plane-class fallback"
NO_ARTIFACT = ("no stackup artifact (<board stem>.stackup.json beside the board, or "
               "stackup_json): run as before, without the stackup's layer roles (ADR D7.4); "
               "build_board_from_netlist or resolve_stackup writes one")
NOT_DECLARED = ("the netlist declares no stackup or copper_layers and has no <netlist "
                "stem>.stackup.json: built as before (the default stackup), without the "
                "artifact or the managed rules file (ADR D7.4); resolve_stackup writes them")
PAD_LAYER_FIX = ("add the pad layer to the net's net_specs layers, or route the net by "
                 "preroute / agent routing (ADR D7.9 pad-layer rule)")
_PAD_NET = re.compile(r"\bpad\b[^\[\]]*\[([^\]]*)\]", re.IGNORECASE)
_ERROR_LINE = re.compile(r"(^|\s)Error:", re.MULTILINE)


def _tool(name: str) -> ModuleType:
    """server/tools/<name>, imported on first use."""
    tools = str(tc.TOOLS_DIR)
    if tools not in sys.path:
        sys.path.insert(0, tools)
    return importlib.import_module(name)


def _same_file(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _atomic_write(path: Path, data: bytes) -> None:
    handle, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# --- lookup (ADR D7.4) ------------------------------------------------------------------
@dataclass(frozen=True)
class Found:
    """An artifact a board tool uses: its path and its content."""
    path: str
    doc: Mapping[str, Any]


def netlist_artifact(netlist: str | os.PathLike[str]) -> Path:
    """<netlist stem>.stackup.json beside the netlist (physics_resolve.artifact_path)."""
    path = Path(netlist)
    stem = path.with_suffix("") if path.suffix == NETLIST_SUFFIX else path
    return stem.with_name(stem.name + ARTIFACT_SUFFIX)


def board_artifact(pcb: str | os.PathLike[str]) -> Path:
    """<board stem>.stackup.json beside the board."""
    path = Path(pcb)
    return path.with_name(path.stem + ARTIFACT_SUFFIX)


def artifact_for(*, netlist: str = "", pcb: str = "", explicit: str = "") -> str | None:
    """The artifact path a tool uses: `explicit` (ValueError when it is not a file), else the
    netlist's, else the board's; None when that file does not exist."""
    if explicit:
        if not Path(explicit).is_file():
            raise ValueError(f"stackup_json {explicit} is not a file")
        return str(explicit)
    if not (netlist or pcb):
        raise ValueError("artifact_for needs a netlist, a board or an explicit stackup_json")
    path = netlist_artifact(netlist) if netlist else board_artifact(pcb)
    return str(path) if path.is_file() else None


def read_artifact(path: str | os.PathLike[str]) -> dict[str, Any]:
    """A physical@1 artifact; ValueError when it is not one, OSError when unreadable."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"{path} is not JSON: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ValueError(f"{path} is not a {SCHEMA} artifact")
    return doc


def find(pcb: str, explicit: str = "") -> Found | dict | None:
    """The artifact of a board-only tool: Found, None (today's behaviour), or the refusal
    {ok: False, reason} for a stackup_json that is missing or not an artifact, or a board-side
    artifact that cannot be read."""
    try:
        path = artifact_for(pcb=pcb, explicit=explicit)
        return None if path is None else Found(path, read_artifact(path))
    except (OSError, ValueError) as exc:
        return {"ok": False, "reason": f"stackup artifact: {exc}"}


def no_artifact() -> dict[str, Any]:
    """The result note of a board-only tool that ran without an artifact."""
    return {"physics": {"artifact": None, "note": NO_ARTIFACT}}


# --- build_board_from_netlist -----------------------------------------------------------
def declared(design: Mapping[str, Any]) -> bool:
    """Does the netlist declare its physical build (a stackup or copper_layers)?"""
    return any(design.get(key) is not None for key in DECLARED_KEYS)


def write_mirror(artifact: Path, out_pcb: str) -> str | None:
    """The byte-identical <board stem>.stackup.json beside `out_pcb` (written only when its
    bytes differ); None when that is the artifact itself."""
    mirror = board_artifact(out_pcb)
    if _same_file(mirror, artifact):
        return None
    data = artifact.read_bytes()
    with contextlib.suppress(OSError):
        if mirror.read_bytes() == data:
            return os.path.abspath(mirror)
    _atomic_write(mirror, data)
    return os.path.abspath(mirror)


def drop_mirror(out_pcb: str) -> dict[str, str]:
    """A build without an artifact removes a <board stem>.stackup.json left beside the board
    by an earlier build (the board-only tools would apply its stale roles): {removed} or
    {warning} when it cannot be removed; {} when there is none."""
    mirror = board_artifact(out_pcb)
    if not mirror.is_file():
        return {}
    try:
        mirror.unlink()
    except OSError as exc:
        return {"warning": f"could not remove the stale {mirror}: {exc}"}
    return {"removed": os.path.abspath(mirror)}


def _ensure_record(path: Path, why: str | None, written: Mapping[str, Any] | None,
                   out_pcb: str) -> dict[str, Any]:
    if written is None:                       # fresh: the artifact's own notes
        doc = read_artifact(path)
        notes = {"warnings": doc.get("warnings") or [], "errors": doc.get("errors") or []}
    else:
        notes = {"warnings": written["warnings"], "errors": written["errors"],
                 "decisions_written": written["decisions_written"]}
    return {"artifact": os.path.abspath(path), "resolved": written is not None,
            "stale_reason": why, "dru_path": os.path.abspath(Path(out_pcb).with_suffix(
                ".kicad_dru")), **notes}


def ensure_artifact(netlist: str, out_pcb: str, *,
                    design: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """build_board_from_netlist's artifact, before the SWIG child (module doc): {"physics":
    record} or the refusal {ok: False, reason, physics}. `design` defaults to the netlist."""
    pr = _tool("physics_resolve")
    if design is None:
        design = json.loads(Path(netlist).read_text(encoding="utf-8-sig"))
    path = netlist_artifact(netlist)
    if not declared(design) and not path.is_file():
        return {"physics": {"artifact": None, "note": NOT_DECLARED, **drop_mirror(out_pcb)}}
    try:
        why = pr.staleness(path, design, board_path=out_pcb)
        written = pr.write_artifact(netlist, design, board_path=out_pcb) if why else None
        record = _ensure_record(path, why, written, out_pcb)
    except (OSError, ValueError, TypeError) as exc:
        return {"ok": False, "reason": f"stackup artifact {path} not written (ADR D7.4; "
                                       f"nothing was built): {exc}"}
    if record["errors"]:
        return {"ok": False, "physics": record,
                "reason": f"stackup artifact {path} has errors (ADR D7.4; nothing was built): "
                          + "; ".join(record["errors"])}
    try:
        return {"physics": {**record, "mirror": write_mirror(path, out_pcb)}}
    except OSError as exc:
        return {"ok": False, "physics": record, "reason": f"cannot write the board-stem "
                f"mirror of {path} (nothing was built): {exc}"}


def builder_env(env: Mapping[str, str], ensured: Mapping[str, Any]) -> dict[str, str]:
    """The builder's environment: `env` without an inherited KICAD_EDA_STACKUP, plus the
    ensured artifact when there is one."""
    base = {k: v for k, v in env.items() if k.upper() != ENV_VAR}
    artifact = (ensured.get("physics") or {}).get("artifact")
    return {**base, ENV_VAR: artifact} if artifact else base


# --- autoroute_board ---------------------------------------------------------------------
def route_policy(pcb: str, explicit: str = "") -> dict[str, Any]:
    """autoroute_board's layer policy: {} without an artifact (today's route), {artifact, dsn}
    with one, or the refusal {ok: False, step: "dsn", reason}."""
    found = find(pcb, explicit)
    if found is None:
        return {}
    if isinstance(found, dict):
        return {**found, "step": "dsn"}
    dsn = found.doc.get("dsn")
    if not isinstance(dsn, Mapping):
        return {"ok": False, "step": "dsn", "reason": f"stackup artifact {found.path} has no "
                "DSN layer policy (its stackup did not resolve: fix the ST-E errors and "
                "rebuild the board)"}
    return {"artifact": found.path, "dsn": dict(dsn)}


def apply_dsn_policy(dsn: str, policy: Mapping[str, Any],
                     variant: str | None = None) -> dict[str, Any]:
    """dsn_layers.postprocess on the exported DSN (in place): {} without a policy,
    {dsn_policy: report} or the refusal {ok: False, step: "dsn", reason[, dsn_policy,
    pad_layer_excluded]}."""
    if not policy.get("dsn"):
        return {}
    dl = _tool("dsn_layers")
    try:
        report = dl.postprocess_file(dsn, policy["dsn"], variant=variant)
    except dl.DsnLayersError as exc:
        return {"ok": False, "step": "dsn", "dsn": dsn,
                "reason": f"the DSN layer policy of {policy['artifact']} does not fit: {exc}"}
    report = {**report, "artifact": policy["artifact"]}
    excluded = report.get("pad_layer_excluded") or {}
    if not excluded:
        return {"dsn_policy": report}
    nets = ", ".join(f"{net} ({'/'.join(layers)})" for net, layers in sorted(excluded.items()))
    return {"ok": False, "step": "dsn", "dsn": dsn, "dsn_policy": report,
            "pad_layer_excluded": excluded,
            "reason": f"impedance net(s) with SMD pads outside their routed layers: {nets}; "
                      f"Freerouting cannot fan them out: {PAD_LAYER_FIX}"}


def ses_args(policy: Mapping[str, Any]) -> list[str]:
    """swig_import_ses.py's optional argv[3]: the artifact (role-aware GND pours)."""
    return [str(policy["artifact"])] if policy.get("artifact") else []


def _pad_nets(entry: Any) -> set[str]:
    items = entry.get("items") if isinstance(entry, Mapping) else None
    found = (_PAD_NET.search(str(i.get("description", ""))) for i in (items or [])
             if isinstance(i, Mapping))
    return {m.group(1) for m in found if m}


def plane_unconnected(pcb: str, policy: Mapping[str, Any]) -> dict[str, int]:
    """{plane net: unconnected items with a pad of it} in the fresh DRC report of `pcb`
    (drc_guard's <stem>_drc.json); {} when there is none or it cannot be read."""
    planes = set((policy.get("dsn") or {}).get("plane_nets") or {})
    try:
        report = json.loads(_tool("drc_guard").report_path(pcb).read_text(encoding="utf-8"))
        entries = report.get("unconnected_items") or []
    except (OSError, ValueError, AttributeError):
        return {}
    counts: dict[str, int] = {}
    for entry in entries:
        for net in _pad_nets(entry) & planes:
            counts = {**counts, net: counts.get(net, 0) + 1}
    return dict(sorted(counts.items()))


def plane_fallback(pcb: str, policy: Mapping[str, Any],
                   attempt: Callable[[str | None], dict]) -> dict:
    """autoroute_board's route (attempt(variant): one export -> route -> import -> DRC run):
    as before without an artifact (+ the note) or without plane nets; else the restricted
    plane classes first and, when they leave a pad of a plane net unconnected, exactly one
    re-route of the pre-route board with the unrestricted variant (module doc)."""
    if not policy.get("artifact"):
        return {**attempt(None), **no_artifact()}
    record = {"physics": {"artifact": policy["artifact"]}}
    dsn = policy.get("dsn") or {}
    if not dsn.get("plane_nets") or dsn.get("plane_classes") == UNRESTRICTED:
        return {**attempt(None), **record}
    backup = pcb + PREROUTE_SUFFIX
    shutil.copyfile(pcb, backup)
    try:
        first = attempt(None)
        before = plane_unconnected(pcb, policy) if first.get("unconnected") else {}
        if not before:
            return {**first, **record}
        return {**reroute(pcb, backup, first, before, policy, attempt), **record}
    finally:
        Path(backup).unlink(missing_ok=True)


def _better(second: Mapping[str, Any], first: Mapping[str, Any]) -> bool:
    after = second.get("unconnected")
    return isinstance(after, int) and after < first["unconnected"]


def reroute(pcb: str, backup: str, first: dict, before: dict[str, int],
            policy: Mapping[str, Any], attempt: Callable[[str | None], dict]) -> dict:
    """The T10 re-route of the pre-route board (`backup`) with the unrestricted variant; the
    restricted route comes back when the re-route is not better."""
    kept, better = pcb + FIRST_ROUTE_SUFFIX, False
    shutil.copyfile(pcb, kept)
    try:
        shutil.copyfile(backup, pcb)
        second = attempt(UNRESTRICTED)
        after = plane_unconnected(pcb, policy) if second.get("unconnected") else {}
        better = _better(second, first)
    finally:                                   # also when the re-route raised
        if not better:
            shutil.copyfile(kept, pcb)
        Path(kept).unlink(missing_ok=True)
    chosen = second if better else first
    fallback = fallback_record(first, second, before, after, better)
    return {**chosen, "dsn_policy": {**(chosen.get("dsn_policy") or {}), "fallback": fallback},
            "decision_drafts": [fallback_draft(fallback)]}


def fallback_record(first: Mapping[str, Any], second: Mapping[str, Any],
                    before: Mapping[str, int], after: Mapping[str, int],
                    better: bool) -> dict[str, Any]:
    """dsn_policy.fallback of a T10 re-route."""
    nets = ", ".join(f"{net} ({n})" for net, n in before.items())
    failed = {k: second[k] for k in ("step", "reason") if k in second}
    return {"reason": f"Freerouting left {sum(before.values())} pad connection(s) of plane "
                      f"net(s) {nets} unconnected with the plane classes restricted to the "
                      "signal layers plus their own plane",
            "variant": UNRESTRICTED, "unconnected_before": first.get("unconnected"),
            "unconnected_after": second.get("unconnected"),
            "plane_unconnected_before": dict(before), "plane_unconnected_after": dict(after),
            "kept": UNRESTRICTED if better else RESTRICTED,
            **({"rerouted": failed} if second.get("ok") is False and failed else {}),
            **({} if better else {"note": "the restricted route was kept; the .dsn / .ses on "
                                          "disk are the unrestricted run's"})}


def fallback_draft(fallback: Mapping[str, Any]) -> dict[str, Any]:
    """The G4 decision draft (decisions.append_decision fields without id / ts)."""
    return {"stage": "G4",
            "question": "Freerouting left pads of plane nets unconnected with the plane nets "
                        "restricted to the signal layers plus their own plane: re-route with "
                        "the plane nets unrestricted?",
            "decision": f"re-routed once with the unrestricted plane classes and kept the "
                        f"{fallback['kept']} route ({fallback['unconnected_before']} -> "
                        f"{fallback['unconnected_after']} unconnected)",
            "rationale": f"{fallback['reason']}; the unrestricted variant still keeps every "
                         "other net off the plane layers (ADR D7.9)",
            "alternatives": ["keep the restricted route and connect the plane-net pads by "
                             "agent routing",
                             "give an inner layer the mixed role, or add layers, so the plane "
                             "nets fan out on routable copper"],
            "confidence": "med", "source": FALLBACK_SOURCE, "reversible": True,
            "override": "stackup"}


# --- add_ground_stitching ----------------------------------------------------------------
def stitch_args(pcb: str, explicit: str = "") -> list[str] | dict:
    """emc_stitch.py's ``--stackup=<artifact>`` ([] without one), or the refusal."""
    found = find(pcb, explicit)
    if isinstance(found, dict):
        return found
    return [] if found is None else [f"{STITCH_FLAG}={found.path}"]


def stitch_record(args: list[str]) -> dict[str, Any]:
    """add_ground_stitching's "physics": the artifact used, or the no-artifact note."""
    return {"physics": {"artifact": args[0].partition("=")[2]}} if args else no_artifact()


# --- export_fab_package (ADR D7.12, R24) -------------------------------------------------
def _gbrjob(fs: ModuleType, job: Path, doc: Mapping[str, Any]) -> tuple[dict, list[str]]:
    if not job.is_file():
        return {"path": None}, [f"no job file {job.name} beside the Gerbers: the stackup "
                                "cannot reach the fab"]
    try:
        fixed = fs.fix_gbrjob_file_attributes(job, job.parent)
        check = fs.check_gbrjob(job, doc)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"path": str(job)}, [f"job file {job.name}: {exc}"]
    return ({"path": str(job), "changed": fixed["changed"], "rows": check["rows"],
             "expected_rows": check["expected_rows"], "notes": check["notes"]},
            [*fixed["problems"], *check["problems"]])


def _ipc2581(fs: ModuleType, pcb: str, xml: Path, doc: Mapping[str, Any],
             cli: str) -> tuple[dict | None, list[str]]:
    try:
        reasons = fs.ipc2581_reasons(doc)
    except (ValueError, TypeError, KeyError) as exc:
        return None, [f"IPC-2581 rule not evaluated: {exc}"]
    if not reasons:
        return None, []
    xml.unlink(missing_ok=True)
    try:
        r = tc.run([cli, "pcb", "export", "ipc2581", "-o", str(xml), pcb],
                   timeout=IPC2581_TIMEOUT_S)
        out = (r.stdout or "") + (r.stderr or "")
        bad = r.returncode != 0 or bool(_ERROR_LINE.search(out)) or not xml.is_file()
    except (OSError, subprocess.TimeoutExpired) as exc:
        out, bad = f"{type(exc).__name__}: {exc}", True
    if bad:
        return {"path": None, "reasons": reasons}, [f"IPC-2581 export failed: "
                                                    f"{out.strip()[-300:]}"]
    return {"path": str(xml), "reasons": reasons}, []


def _design(netlist: str) -> Mapping[str, Any] | None:
    try:
        doc = json.loads(Path(netlist).read_text(encoding="utf-8-sig")) if netlist else None
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, Mapping) else None


def _stackup_files(fs: ModuleType, out: Path, stem: str, doc: Mapping[str, Any],
                   netlist: str) -> tuple[dict, list[str]]:
    try:
        return fs.write_outputs(out, stem, doc, design=_design(netlist)), []
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"csv": None, "notes": None}, [f"stackup CSV / fab notes not written: {exc}"]


def fab_stackup_outputs(pcb: str, out_dir: str, artifact: Found | None, cli: str, *,
                        netlist: str = "") -> dict[str, Any]:
    """The fab outputs that carry the stackup, written into `out_dir` before the zip (module
    doc): {} without an artifact, else {"fab_stackup": {artifact, gbrjob, ipc2581, csv,
    notes, problems}}."""
    if artifact is None:
        return {}
    fs, out, stem = _tool("fab_stackup"), Path(out_dir), Path(pcb).stem
    job, job_problems = _gbrjob(fs, out / f"{stem}{fs.GBRJOB_SUFFIX}", artifact.doc)
    ipc, ipc_problems = _ipc2581(fs, pcb, out / f"{stem}{fs.IPC2581_SUFFIX}", artifact.doc, cli)
    files, file_problems = _stackup_files(fs, out, stem, artifact.doc, netlist)
    return {"fab_stackup": {"artifact": artifact.path, "gbrjob": job, "ipc2581": ipc, **files,
                            "problems": [*job_problems, *ipc_problems, *file_problems]}}


def with_fab_stackup(res: dict, stack: Mapping[str, Any]) -> dict:
    """export_fab_package's result with the stackup outputs: unchanged without them; their
    problems make it ok False with a reason (G5 blockers, R24)."""
    if not stack:
        return res
    problems = stack["fab_stackup"]["problems"]
    out = {**res, **stack, "ok": bool(res.get("ok")) and not problems}
    if not problems:
        return out
    return {**out, "reason": "fab stackup outputs (ADR D7.12): " + "; ".join(problems)}
