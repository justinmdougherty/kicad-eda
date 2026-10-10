"""Stackup, impedance and RF physics tools (D7; docs/ROADMAP_DECISIONS.md D7).

The MCP surface for a design's physical layer, computed from the stackup the
netlist declares (any copper layer count, per-layer copper, per-dielectric
material / thickness / Dk / Df; fab presets only expand into that model).
Registered empty by X-04; **D7-13a** fills TOOLS (then D7-13b, D7-08b):

  * ``resolve_stackup``: resolve the declared stackup, the per-net
    transmission lines and the DRU/DSN rules into ``<stem>.stackup.json``
    and the managed ``<board stem>.kicad_dru``
    (server/tools/physics_resolve.py; ``write=False`` is a dry run);
  * ``impedance_calc``: microstrip, embedded microstrip, stripline, CPWG and
    coplanar, single-ended and differential, reference planes taken from the
    stackup (a netlist's, a fab preset, a stackup object, else the default
    4-layer build); a given width is checked instead of solved; ``net`` reports
    one netlist net as the resolver sizes it;
  * ``list_materials`` / ``list_stackup_presets``: the laminate and fab
    preset data the stackup model accepts;
  * ``check_layer_count``: the layer count as a recorded design decision
    (declared count and rationale, the D7.11 floors, the napkin default;
    D7-08b adds its score);
  * ``rf_plan`` / ``coexistence_check`` (D7-13b): RF chains, antennas and
    radio coexistence.

Every tool returns a dict with ``ok``; a refused request or a bad input is
{"ok": False, "reason"} (plus what was learnt before the refusal). Relative
board paths resolve against the netlist's folder. server/tools modules are
imported inside the tools, never at import time, and nothing here starts a
process (FD runs in-process within KICAD_EDA_FD_BUDGET_S).

Plain functions; kicad_server registers every function in TOOLS with FastMCP
(this module is listed in kicad_server.TOOL_MODULES). A new tool is added by
appending it to TOOLS — kicad_server.py is not edited.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

TOOLS_DIR = Path(__file__).resolve().parent / "tools"
KINDS = ("single", "diff")
FD_MODES = ("off", "auto", "force")
LINE_NAME = "LINE"                       # the net name of an impedance_calc line
TARGET_KEYS = ("impedance", "diff")
LINE_SUMMARY_KEYS = ("width", "pair_gap", "ground_gap", "width_window", "z_achieved")
PRESET_LIMIT_MAX = 1000
MAX_COPPER = 32
_ERRORS = (ValueError, TypeError, OSError, ImportError, LookupError)


class ToolError(ValueError):
    """A refused request: returned as {"ok": False, "reason", **extra}."""

    def __init__(self, reason: str, **extra: Any) -> None:
        super().__init__(reason)
        self.extra = dict(extra)


# --- shared helpers -------------------------------------------------------------------------
def _tool(name: str) -> ModuleType:
    """A server/tools module, imported on first use (server/tools put on sys.path then)."""
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))
    return importlib.import_module(name)


def _guarded(body: Callable[..., dict], *args: Any) -> dict:
    """body(*args); a refusal or a bad input becomes {"ok": False, "reason", ...}."""
    try:
        return body(*args)
    except _ERRORS as exc:
        reason = str(exc) or type(exc).__name__
        if isinstance(exc, ImportError):
            reason = (f"a Python dependency is missing ({exc}); install "
                      "server/requirements.txt into the server's Python")
        return {**getattr(exc, "extra", {}), "ok": False, "reason": reason}


def _envelope(problems: list[str], /, **data: Any) -> dict:
    """{"ok": no problems, "reason" (when not ok), **data}."""
    head: dict = {"ok": not problems}
    if problems:
        more = f" (+{len(problems) - 3} more)" if len(problems) > 3 else ""
        head["reason"] = f"{len(problems)} error(s): " + "; ".join(problems[:3]) + more
    return {**head, **data}


def _netlist(netlist: Any) -> tuple[Path, dict]:
    if not isinstance(netlist, str) or not netlist.strip():
        raise ToolError(f"netlist must be the path of the design's netlist.json, got {netlist!r}")
    path = Path(os.path.abspath(netlist.strip()))
    if not path.is_file():
        raise ToolError(f"netlist not found: {path}")
    try:
        design = json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ToolError(f"{path} is not a JSON netlist: {exc}") from exc
    if not isinstance(design, dict):
        raise ToolError(f"{path} is not a JSON object netlist (got {type(design).__name__})")
    return path, design


def _check_fd(fd: Any) -> str:
    if fd not in FD_MODES:
        raise ToolError(f"fd must be one of {', '.join(FD_MODES)} (FD verification tier), "
                        f"got {fd!r}")
    return fd


def _board(board: Any, netlist: Path) -> Path | None:
    if board is None or (isinstance(board, str) and not board.strip()):
        return None
    if not isinstance(board, str):
        raise ToolError(f"board must be a .kicad_pcb path, got {board!r}")
    path = Path(board.strip())
    return path if path.is_absolute() else netlist.parent / path


def _number(name: str, value: Any, *, positive: bool = False) -> float:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if not ok or value < 0 or (positive and value == 0):
        what = "a number > 0" if positive else "a number >= 0 (0 = not given)"
        raise ToolError(f"{name} must be {what}, got {value!r}")
    return float(value)


def _issue_texts(issues: Any) -> list[str]:
    return [f"{i['id']} at {i['path']}: {i['message']}" for i in issues or []
            if i.get("severity") == "error"]


# --- summaries ------------------------------------------------------------------------------
def _stackup_summary(st: Mapping[str, Any] | None) -> dict | None:
    if st is None:
        return None
    copper = [{k: c[k] for k in ("name", "role", "net", "kicad_type", "thickness", "oz")}
              for c in st["copper"]]
    dielectrics = [{"name": d["name"], "kind": d["kind"], "thickness": d["thickness"],
                    "er": [p["er"] for p in d["plies"]],
                    "material": [p["material_id"] or p["label"] for p in d["plies"]]}
                   for d in st["dielectrics"]]
    keys = ("source", "origin", "preset", "fab", "copper_layers", "kicad_copper_count",
            "board_thickness_computed", "finish", "impedance_controlled", "rationale")
    return {**{k: st[k] for k in keys}, "mask": st["mask"]["source"], "copper": copper,
            "dielectrics": dielectrics, "warnings": list(st["warnings"])}


def _line_summary(rec: Mapping[str, Any]) -> dict:
    primary = (rec.get("layers") or {}).get(rec.get("primary_layer")) or {}
    keys = ("kind", "line", "primary_layer", "z_target", "status", "accuracy_class")
    return {**{k: rec.get(k) for k in keys},
            **{k: primary.get(k) for k in LINE_SUMMARY_KEYS},
            "layers": list(rec.get("layers") or {}), "errors": _issue_texts(rec.get("issues"))}


def _artifact_summary(art: Mapping[str, Any]) -> dict:
    return {"stackup": _stackup_summary(art["stackup"]),
            "lines": {net: _line_summary(rec) for net, rec in art["lines"].items()},
            "dsn": art["dsn"], "dru": art["dru"], "decision_drafts": len(art["decision_drafts"]),
            "design_digest": art["design_digest"], "data_version": art["data_version"],
            "warnings": art["warnings"]}


# --- resolve_stackup ------------------------------------------------------------------------
def _resolve_stackup(netlist: Any, board: Any, fd: Any, write: Any) -> dict:
    path, design = _netlist(netlist)
    _check_fd(fd)
    pr = _tool("physics_resolve")
    board_path = _board(board, path)
    if write:
        out = pr.write_artifact(path, design, board_path=board_path, fd=fd)
        art = pr.read_artifact(out["path"])
        meta = {k: out[k] for k in ("path", "dru_path", "changed", "decisions_written", "fd",
                                    "previous")}
        return _envelope(out["errors"], **meta, written=True, dru_written=out["dru"],
                         **_artifact_summary(art), errors=out["errors"])
    budget = pr.make_budget(fd)
    art = pr.resolve_design(design, path, board_path=board_path, fd=fd, budget=budget,
                            cache=pr.cached_fd(path))
    dru = _tool("dru_writer").dru_path_for(board_path or pr.default_board(path))
    meta = {"path": None, "dru_path": str(dru), "changed": False, "decisions_written": 0,
            "fd": {"mode": fd, "spent_s": 0.0 if budget is None else round(budget.spent, 3)}}
    return _envelope(art["errors"], **meta, written=False, **_artifact_summary(art),
                     errors=art["errors"])


def resolve_stackup(netlist: str, board: str = "", fd: str = "auto", write: bool = True) -> dict:
    """Resolve the netlist's stackup, impedance lines, DSN layer policy and managed DRU rules
    (ADR D7.4) and write <netlist stem>.stackup.json plus the managed block of
    <board stem>.kicad_dru (board default: <netlist stem>.kicad_pcb, the pipeline's; a
    relative board resolves against the netlist folder). New decision drafts (defaulted
    plane nets, lines FD could not verify) are appended to <netlist stem>.decisions.jsonl
    once. fd: off | auto (FD verification when numpy/scipy are present, cached in the
    artifact, within KICAD_EDA_FD_BUDGET_S) | force. write=False resolves without writing
    anything. Returns the stackup, per-net line summary, dsn, dru, warnings and errors; ok is
    False when the design has errors (they are recorded in the artifact)."""
    return _guarded(_resolve_stackup, netlist, board, fd, write)


# --- impedance_calc -------------------------------------------------------------------------
def _stripped(design: Mapping[str, Any], keep: str | None = None) -> dict:
    """The design's net_specs without impedance / diff targets (except net `keep`)."""
    specs = design.get("net_specs")
    out = {}
    for net, spec in (specs.items() if isinstance(specs, Mapping) else ()):
        out[net] = spec if net == keep or not isinstance(spec, Mapping) else {
            k: v for k, v in spec.items() if k not in TARGET_KEYS}
    return out


def _source(netlist: str, preset: str, stackup_json: str) -> dict:
    """The base design of a free line: a netlist's, a preset, a stackup object or nothing."""
    given = [n for n, v in (("netlist", netlist), ("preset", preset),
                            ("stackup_json", stackup_json)) if isinstance(v, str) and v.strip()]
    if len(given) > 1:
        raise ToolError(f"give at most one stackup source (netlist, preset or stackup_json), "
                        f"got {', '.join(given)}")
    if netlist and netlist.strip():
        design = _netlist(netlist)[1]
        return {**design, "net_specs": _stripped(design)}
    if preset and preset.strip():
        return {"stackup": {"preset": preset.strip()}}
    if stackup_json and stackup_json.strip():
        try:
            return {"stackup": json.loads(stackup_json)}
        except ValueError as exc:
            raise ToolError(f"stackup_json is not JSON: {exc}") from exc
    return {}


def _line_spec(target: float, kind: str, layer: str, line: str, dims: Mapping[str, float],
               freq_hz: float) -> dict:
    spec: dict[str, Any] = {"impedance" if kind == "single" else "diff": target,
                            "layers": [layer], "line": line or "auto"}
    for key, value in dims.items():
        if value > 0 and (key != "gap" or kind == "diff"):
            spec[key] = value
    return {**spec, **({"freq_hz": freq_hz} if freq_hz > 0 else {})}


def _names(value: Any) -> set[str]:
    """The string names of a netlist 'nets' / 'net_specs' value (a map or a list)."""
    return {n for n in (value if isinstance(value, (Mapping, list)) else ())
            if isinstance(n, str)}


def _free_name(design: Mapping[str, Any]) -> str:
    taken = _names(design.get("nets")) | _names(design.get("net_specs"))
    return next(n for k in range(len(taken) + 1)
                if (n := LINE_NAME if k == 0 else f"{LINE_NAME}_{k}") not in taken)


def _calc_design(args: Mapping[str, Any]) -> tuple[dict, str]:
    """(the design holding the line, its net name) for impedance_calc's arguments."""
    if args["net"]:
        if not args["netlist"]:
            raise ToolError("net needs the netlist that declares it (netlist=...)")
        design = _netlist(args["netlist"])[1]
        specs = design.get("net_specs")
        spec = specs.get(args["net"]) if isinstance(specs, Mapping) else None
        if not isinstance(spec, Mapping) or not any(k in spec for k in TARGET_KEYS):
            raise ToolError(f"net {args['net']!r} has no impedance or diff target in the "
                            "netlist's net_specs")
        return {**design, "net_specs": _stripped(design, args["net"])}, args["net"]
    target = _number("target_ohm", args["target_ohm"], positive=True)
    if args["kind"] not in KINDS:
        raise ToolError(f"kind must be one of {', '.join(KINDS)}, got {args['kind']!r}")
    dims = {"width": _number("width_mm", args["width_mm"]),
            "gap": _number("pair_gap_mm", args["pair_gap_mm"]),
            "ground_gap": _number("ground_gap_mm", args["ground_gap_mm"])}
    freq = _number("freq_hz", args["freq_hz"])
    base = _source(args["netlist"], args["preset"], args["stackup_json"])
    name = _free_name(base)
    spec = _line_spec(target, args["kind"], args["layer"], args["line"], dims, freq)
    return {**base, "net_specs": {**(base.get("net_specs") or {}), name: spec}}, name


def _impedance_calc(args: Mapping[str, Any]) -> dict:
    fd = _check_fd(args["fd"])
    design, name = _calc_design(args)
    rs, issues = _tool("stackup_model").resolve(design)
    if rs is None:
        raise ToolError("the stackup does not resolve: " + "; ".join(
            str(i) for i in issues if i.severity == "error"))
    rec = _tool("impedance").resolve_impedance(design, fd=fd, errors="record")[name]
    errors = _issue_texts(rec["issues"])
    primary = rec.get("primary_layer")
    data = {"analysis": "check" if (args["width_mm"] or 0) > 0 and not args["net"] else
            "synthesis", "net": args["net"] or None, "kind": rec["kind"],
            "target_ohm": rec["z_target"], "layer": primary,
            "result": (rec.get("layers") or {}).get(primary), "layers": rec.get("layers") or {},
            "issues": rec["issues"], "warnings": rec["warnings"],
            "decision_drafts": rec["decision_drafts"],
            "stackup": _stackup_summary(rs.to_dict())}
    return _envelope(errors, **data)


def impedance_calc(target_ohm: float = 50.0, kind: str = "single", layer: str = "F.Cu",
                   line: str = "auto", width_mm: float = 0.0, pair_gap_mm: float = 0.0,
                   ground_gap_mm: float = 0.0, freq_hz: float = 0.0, netlist: str = "",
                   net: str = "", preset: str = "", stackup_json: str = "",
                   fd: str = "off") -> dict:
    """Size (or, with width_mm, check) one transmission line on a stackup (ADR D7.6):
    kind single (target_ohm = Z0) or diff (Zdiff; pair_gap_mm, default = solved with the
    width); line auto | microstrip | cpwg | cpw | stripline | dual_stripline |
    embedded_microstrip on copper `layer`; references, plies and solder mask come from the
    stackup: the netlist's (netlist=), a fab preset ("jlcpcb:JLC04161H-3313"), a stackup
    object as JSON (stackup_json, the netlist 'stackup' value, legacy {h, er, t} included),
    else the default 4-layer jlcpcb:JLC04161H-7628 build. ground_gap_mm sets a CPWG gap,
    freq_hz the dispersion frequency. With netlist= and net=, the netlist's own net is
    reported as the resolver sizes it (every allowed layer). fd off (default) | auto | force.
    Returns the primary layer's line record (width, gaps, z_achieved, model, accuracy class,
    status, width_window, references), every layer's record and the stackup summary."""
    args = {"target_ohm": target_ohm, "kind": kind, "layer": layer, "line": line,
            "width_mm": width_mm, "pair_gap_mm": pair_gap_mm, "ground_gap_mm": ground_gap_mm,
            "freq_hz": freq_hz, "netlist": netlist or "", "net": net or "",
            "preset": preset or "", "stackup_json": stackup_json or "", "fd": fd}
    return _guarded(_impedance_calc, args)


# --- list_materials -------------------------------------------------------------------------
def _point(entry: Mapping[str, Any]) -> dict:
    keys = ("value", "tol", "freq_hz", "basis", "when", "fab")
    return {k: entry[k] for k in keys if k in entry}


def _material_row(mid: str, m: Mapping[str, Any]) -> dict:
    keys = ("vendor", "product", "family", "category", "forms", "ptfe", "process", "tg_c",
            "standard_thicknesses_mm")
    return {"id": mid, **{k: m.get(k) for k in keys},
            "dk": [_point(e) for e in m.get("dk") or []],
            "df": [_point(e) for e in m.get("df") or []]}


def _matches(query: str, *fields: Any) -> bool:
    q = query.strip().lower()
    return not q or any(q in str(f).lower() for f in fields if f is not None)


def _list_materials(category: Any, query: Any, material: Any) -> dict:
    mat = _tool("materials")
    lib = mat.load_library()
    if material:
        found = lib.get(material)
        if found is None:
            raise ToolError(f"unknown material {material!r} (list_materials lists the ids)")
        return _envelope([], material={"id": material, **found}, data_version=lib.data_version)
    if category and category not in mat.CATEGORIES:
        raise ToolError(f"category must be one of {', '.join(mat.CATEGORIES)}, got {category!r}")
    rows = [_material_row(mid, lib.materials[mid]) for mid in lib.ids()]
    rows = [r for r in rows if (not category or r["category"] == category)
            and _matches(query or "", r["id"], r["vendor"], r["product"], r["family"])]
    return _envelope([], count=len(rows), materials=rows, categories=list(mat.CATEGORIES),
                     data_version=lib.data_version,
                     note="a material not listed is allowed with an explicit dk (ADR D7.2.2)")


def list_materials(category: str = "", query: str = "", material: str = "") -> dict:
    """The dielectric materials the stackup model knows (server/data/materials, ADR D7.3):
    id, vendor, product, family, category (fr4 | high-speed | rf-laminate | mask), forms,
    Dk / Df points with frequency and basis (design | process | typical | fab), standard
    thicknesses. Filter by category and a case-insensitive query over id / vendor / product /
    family; material=<id> returns that material's full record with its sources."""
    return _guarded(_list_materials, category, query, material)


# --- list_stackup_presets -------------------------------------------------------------------
def _defaults(fabs: Any) -> dict[str, list[int]]:
    """{preset: [counts]} of every fab's by-count default template."""
    out: dict[str, set[int]] = {}
    for fid in fabs.ids():
        by_count = ((fabs.fabs[fid].get("default_templates") or {}).get("by_count") or {})
        for count, cand in by_count.items():
            preset = cand if ":" in cand else f"{fid}:{cand}"
            out.setdefault(preset, set()).add(int(count))
    return {p: sorted(c) for p, c in out.items()}


def _preset_row(fid: str, name: str, tpl: Mapping[str, Any], defaults: Mapping) -> dict:
    preset = f"{fid}:{name}"
    return {"preset": preset, "fab": fid, "copper_layers": tpl["copper_layers"],
            "thickness_mm": tpl["thickness_mm"], "outer_oz": tpl.get("outer_oz"),
            "inner_oz": tpl.get("inner_oz"), "default_roles": list(tpl["default_roles"]),
            "default_for": defaults.get(preset, [])}


def _fab_row(fid: str, fab: Mapping[str, Any]) -> dict:
    caps = fab.get("capabilities") or {}
    keys = ("layer_counts", "impedance_layer_counts", "impedance_tolerance_pct", "hybrid")
    return {"id": fid, "name": fab.get("name"), **{k: caps.get(k) for k in keys},
            "soldermask": bool(fab.get("soldermask"))}


def _list_presets(fab: Any, copper_layers: Any, query: Any, limit: Any) -> dict:
    fabs = _tool("fab_presets").load_fabs()
    if fab and fab not in fabs:
        raise ToolError(f"unknown fab {fab!r} (known: {', '.join(fabs.ids())})")
    if not (isinstance(copper_layers, int) and not isinstance(copper_layers, bool)
            and (copper_layers == 0 or copper_layers == 1
                 or (copper_layers % 2 == 0 and 2 <= copper_layers <= MAX_COPPER))):
        raise ToolError(f"copper_layers must be 0 (any), 1 or an even count 2..{MAX_COPPER}, "
                        f"got {copper_layers!r}")
    if not (isinstance(limit, int) and not isinstance(limit, bool)
            and 1 <= limit <= PRESET_LIMIT_MAX):
        raise ToolError(f"limit must be 1..{PRESET_LIMIT_MAX}, got {limit!r}")
    defaults, ids = _defaults(fabs), [fab] if fab else list(fabs.ids())
    rows = [_preset_row(fid, name, fabs.template(fid, name), defaults)
            for fid in ids for name in fabs.templates(fid)]
    rows = [r for r in rows if (not copper_layers or r["copper_layers"] == copper_layers)
            and _matches(query or "", r["preset"])]
    rows.sort(key=lambda r: (r["fab"], r["copper_layers"], not r["default_for"], r["preset"]))
    return _envelope([], total=len(rows), truncated=len(rows) > limit, presets=rows[:limit],
                     fabs=[_fab_row(fid, fabs.fabs[fid]) for fid in ids],
                     data_version=fabs.data_version,
                     note=("a preset is a convenience that expands into stackup.layers; any "
                           "stackup (layer count, copper, materials, hybrids) can be declared "
                           "directly (ADR D7.2.2)"))


def list_stackup_presets(fab: str = "", copper_layers: int = 0, query: str = "",
                         limit: int = 50) -> dict:
    """The fab stackup presets (server/data/fabs, ADR D7.3) usable as stackup.preset
    "<fab>:<template>": copper layers, nominal thickness, copper weights, the D7.2.4 default
    roles and the counts it is the fab's default for; plus each fab's layer counts,
    impedance-controlled counts and tolerance. Filter by fab, copper_layers and a
    case-insensitive query on the preset name; at most `limit` rows (total and truncated
    say how many matched)."""
    return _guarded(_list_presets, fab, copper_layers, query, limit)


# --- check_layer_count ----------------------------------------------------------------------
def _resolved(rs: Any) -> dict | None:
    if rs is None:
        return None
    return {"copper_layers": rs.copper_layers, "kicad_copper_count": rs.kicad_copper_count,
            "source": rs.source, "origin": rs.origin, "preset": rs.preset, "fab": rs.fab,
            "rationale": rs.rationale,
            "roles": [[c.name, c.role, c.net] for c in rs.copper]}


def _napkin(nd: Any) -> dict | None:
    if nd is None:
        return None
    return {"copper_layers": nd.copper_layers, "preset": nd.preset, "rationale": nd.rationale,
            "decision_draft": nd.decision_draft, "patch": nd.patch,
            "floors": [f.to_dict() for f in nd.floors]}


def _check_layer_count(netlist: Any) -> dict:
    path, design = _netlist(netlist)
    lp = _tool("layer_policy")
    rs, st_issues = _tool("stackup_model").resolve(design)
    count_issues = [i for i in st_issues if i.id in ("ST-E2", "ST-E7", "ST-W3", "ST-W12")]
    issues = [i.to_dict() for i in (*count_issues, *lp.check_declared(design, rs))]
    feat = lp.features(design)
    errors = [f"{i['id']} at {i['path']}: {i['message']}" for i in issues
              if i["severity"] == "error"]
    errors += [str(i) for i in st_issues if i.severity == "error" and i not in count_issues]
    return _envelope(errors, netlist=str(path), declared=feat["count_declared"],
                     napkin=feat["napkin"], copper_layers=design.get("copper_layers"),
                     rationale=feat["rationale"], resolved=_resolved(rs), issues=issues,
                     floors=[f.to_dict() for f in lp.floors(design, rs)],
                     napkin_default=_napkin(lp.napkin_default(design)))


def check_layer_count(netlist: str) -> dict:
    """The design's copper layer count as a recorded decision (ADR D7.11, wave 2a): the
    declared count (copper_layers, a preset or stackup.layers) and stackup.rationale, the
    resolved stackup's count and roles, the floors that apply (F1 impedance needs the fab's
    controlled-impedance count, F2 stripline needs 6 with the default roles, F3 fine-pitch
    BGA needs 4 / 6), ST-W12 / ST-W3 findings, and in napkin mode with nothing declared the
    default the harness applies (count, preset, rationale, decision draft, netlist patch).
    ok is False only when the count itself is invalid (ST-E2 / ST-E7 / a stackup error)."""
    return _guarded(_check_layer_count, netlist)


TOOLS: tuple[Callable[..., Any], ...] = (
    resolve_stackup,
    impedance_calc,
    list_materials,
    list_stackup_presets,
    check_layer_count,
)
