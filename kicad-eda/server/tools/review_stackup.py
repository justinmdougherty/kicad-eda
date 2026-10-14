#!/usr/bin/env python3
"""The 'Stackup and impedance' section of the hands-off review (ADR D7, task D7-33).

review_report renders the D5 review; X-03 wires this module into its SECTIONS after
'Routing' as ``render(review)`` and appends ``open_items(review)`` to the open-items section.
``review`` is review_report's _Review (attributes ``report``, ``design``, ``decisions``,
``gates``) or a plain mapping with those keys. Everything is read tolerantly: a missing or
odd-shaped key renders a 'not recorded' line, never an exception, and nothing is mutated.

Inputs (what pipeline/stages_d7.py + rp_feature_d7.py write into the pipeline report):
  physics       stage_physics after G2: {artifact, ok, fd, decisions, warnings, errors,
                stackup (stackup_model.ResolvedStackup.to_dict()), lines (impedance records
                per net with .layers per copper layer), rules (the artifact's dru section)}
  dru           the canary (drc_guard.rules_compile, R23): {path, ok, kicad_cli, rc, crash,
                fired, types, reason, others} + the artifact's rules / families / sha256
  layer_roles   G4 swig_layer_role_checks: {judged, ok, issues (R8), warnings (R27 / R28),
                metrics, stackup, board_copper, stackup_copper, pcbnew} or {judged: false,
                reason | skipped | error[, needs]}
  dsn_policy    the Freerouting layer policy: routed_layers / excluded_layers per net
  fab.stackup   G5 fab_stackup_check: {gbrjob {path, rows, expected_rows, notes}, ipc2581
                {path, reasons} | None, csv, notes, zip, problems} or {checked: false, ...}
  layer_policy  the napkin layer default: {applied, copper_layers, preset, decision, ...}
and the decision log (the plane-net defaults ``planes.<layer>`` and the layer-count
decision ``stackup.preset`` are cited by id).

Sub-sections: Stackup (summary lines + the layer table, mask rows included), Impedance (one
row per net and layer with its status; fallback_unverified and closed_form rows are open
items), Design rules, Layer-role checks (G4), Fab outputs (G5). A run that stopped before
the physical-design stage renders the one 'Not produced' line plus what the netlist
declares. Every rendered line is one Markdown line; table cells escape '|'. Stdlib only
(Python 3.11). D7-33b adds the RF rows (report['rf']) to this module.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

SECTION_TITLE = "Stackup and impedance"
STATUS_TEXT = {"fd": "FD verified", "closed_form": "closed form",
               "fallback_unverified": "**UNVERIFIED** (class C fallback)", "error": "no geometry"}
TARGET_TEXT = {"z0": "Z0", "zdiff": "Zdiff"}
SOURCE_TEXT = {"layers": "declared layers", "default": "the ADR D7.2.4 default geometry",
               "policy": "the napkin layer-policy default"}
OPEN_STATUSES = ("fallback_unverified", "closed_form")
NOT_PRODUCED = "- Not produced: the run stopped before the physical-design stage (after G2)."
NOT_RECORDED = ("- Not recorded: the pipeline report has no physics section (the run predates "
                "the D7 stage or ran without the d7 feature): the board carries KiCad's "
                "default stackup and no managed impedance rules.")
NOT_RUN_KEYS = ("board", "autoroute", "drc", "fab", "renders_placement")   # a build happened
LAYER_HEADER = ("Layer", "Type", "Role / kind", "Net / material", "Thickness (mm)",
                "Dk (basis @ GHz)", "Df")
LINE_HEADER = ("Net", "Layer", "Line", "Target", "Width (mm) [window]", "Gap (mm)",
               "Z achieved", "Reference", "Class (est. error)", "Status")
ST_CODE = re.compile(r"\b(ST-[EW]\d+)\b")
LISTED = 6
SHA_SHOWN = 12
GHZ = 1e9


# --- formatting (review_report's conventions) ------------------------------------------------
def _flat(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return " ".join(text.split())


def _cell(value: Any) -> str:
    return _flat(value).replace("|", "\\|") or "-"


def _code(value: Any) -> str:
    text = _flat(value)
    ticks = "`" * (max(map(len, re.findall(r"`+", text)), default=0) + 1)
    pad = " " if text[:1] == "`" or text[-1:] == "`" else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"


def _g(value: Any) -> str:
    """A number with six significant digits and no trailing zeros ('-' when absent)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "-"
    return str(value) if isinstance(value, int) else f"{value:.6g}"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header),
            *("| " + " | ".join(row) + " |" for row in rows)]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _dicts(value: Any) -> list[Mapping[str, Any]]:
    return [v for v in value if isinstance(v, Mapping)] if isinstance(value, (list, tuple)) else []


def _strings(value: Any) -> list[str]:
    return [_flat(v) for v in value] if isinstance(value, (list, tuple)) else []


def _basename(path: Any) -> str:
    return _flat(path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _ghz(freq_hz: Any) -> str:
    return f"{_g(freq_hz / GHZ)} GHz" if isinstance(freq_hz, (int, float)) else "-"


def _field(review: Any, name: str) -> Any:
    if isinstance(review, Mapping):
        return review.get(name)
    return getattr(review, name, None)


# --- the inputs ------------------------------------------------------------------------------
class _Inputs:
    """The report keys this section reads, each shape-checked once."""

    def __init__(self, review: Any) -> None:
        self.report = _mapping(_field(review, "report"))
        self.design = _mapping(_field(review, "design"))
        self.decisions = _dicts(_field(review, "decisions"))
        self.physics = _mapping(self.report.get("physics"))
        self.stackup = _mapping(self.physics.get("stackup"))
        self.lines = {k: _mapping(v) for k, v in _mapping(self.physics.get("lines")).items()}
        self.dru = _mapping(self.report.get("dru"))
        self.roles = _mapping(self.report.get("layer_roles"))
        self.dsn = _mapping(self.report.get("dsn_policy"))
        self.fab = _mapping(self.report.get("fab"))
        self.policy = _mapping(self.report.get("layer_policy"))
        self.gates_ran = {str(_mapping(g).get("gate")) for g in _dicts(self.report.get("gates"))}

    def decision_id(self, override: str) -> str | None:
        """The id of the recorded decision a human changes through `override`."""
        return next((str(d["id"]) for d in self.decisions
                     if d.get("override") == override and d.get("id")), None)

    def copper(self) -> list[Mapping[str, Any]]:
        return _dicts(self.stackup.get("copper"))

    def built(self) -> bool:
        return any(k in self.report for k in NOT_RUN_KEYS) or bool(self.gates_ran & {"G3", "G4",
                                                                                      "G5"})


# --- the stackup: summary lines --------------------------------------------------------------
def _build_line(st: Mapping[str, Any]) -> str:
    count, kicad = st.get("copper_layers"), st.get("kicad_copper_count")
    if st.get("source") == "legacy":
        leg = _mapping(st.get("legacy"))
        return (f"- Build: legacy shorthand h {_g(leg.get('h'))} mm, Er {_g(leg.get('er'))}, "
                f"t {_g(leg.get('t'))} mm (unmasked outer microstrip, ST-W7): no KiCad stackup "
                f"block is written; {_g(count)} copper layers with the default roles")
    preset = st.get("preset")
    source = str(st.get("source") or "unknown")
    what = (f"{SOURCE_TEXT[source]} {_code(preset)}" if source == "policy" and preset else
            f"preset {_code(preset)}" if preset else SOURCE_TEXT.get(source, f"source {source}"))
    layers = f"{_g(count)} copper layers"
    if isinstance(kicad, int) and kicad != count:
        layers += f" (KiCad board: {kicad}" + (", single-sided: B.Cu unused)" if count == 1
                                              else ")")
    return f"- Build: {what} (fab {_flat(st.get('fab') or 'none')}), {layers}"


def _rationale_line(inp: _Inputs) -> str:
    st = inp.stackup
    rationale = _flat(st.get("rationale") or "not stated (declare stackup.rationale)")
    origin = _flat(st.get("origin") or "designer")
    if origin == "layer_policy":
        ident = inp.policy.get("decision") or inp.decision_id("stackup.preset")
        cited = f" recorded as decision {_flat(ident)}" if ident else " (no decision recorded)"
        origin = f"layer_policy, the napkin default{cited}"
    return f"- Layer-count rationale: {rationale} (origin: {origin})"


def _thickness_line(st: Mapping[str, Any]) -> str:
    computed, ordered = st.get("board_thickness_computed"), st.get("board_thickness_ordered")
    tail = f"computed {_g(computed)} mm (copper + dielectrics + 2 solder masks)"
    if not isinstance(ordered, (int, float)):
        return f"- Board thickness: ordered: not stated; {tail}"
    delta = (f": {computed - ordered:+.4f} mm vs ordered"
             if isinstance(computed, (int, float)) else "")
    return f"- Board thickness: ordered {_g(ordered)} mm, {tail}{delta}"


def _plies(st: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [p for d in _dicts(st.get("dielectrics")) for p in _dicts(d.get("plies"))]


def _dk_line(st: Mapping[str, Any]) -> str:
    plies = _plies(st)
    bases = sorted({_flat(p.get("er_basis")) for p in plies if p.get("er_basis")})
    freqs = sorted({p["er_freq_hz"] for p in plies if isinstance(p.get("er_freq_hz"),
                                                                  (int, float))})
    basis = ", ".join(bases) or "not recorded"
    at = " at " + ", ".join(_ghz(f) for f in freqs) if freqs else ""
    return (f"- Dk basis: {basis}{at} (dk_policy {_flat(st.get('dk_policy') or '-')}; design "
            f"frequency {_ghz(st.get('design_freq_hz'))})")


def _mask_line(st: Mapping[str, Any]) -> str:
    mask = _mapping(st.get("mask"))
    source = _flat(mask.get("source") or "not recorded")
    if source == "none":
        return ("- Solder mask: none (legacy shorthand: the lines are computed unmasked)"
                if st.get("source") == "legacy" else "- Solder mask: none (mask: null, ST-W9)")
    er = f"Er {_g(mask.get('dk'))}" + (f" {_flat(mask['basis'])}" if mask.get("basis") else "")
    ref = f"; {_flat(mask['reference'])}" if mask.get("reference") else ""
    return (f"- Solder mask: {source} ({_flat(mask.get('material') or 'lpi')}, {er}, "
            f"{_g(mask.get('over_substrate'))} mm over substrate, {_g(mask.get('over_trace'))} "
            f"mm over trace{ref})")


def _finish_line(st: Mapping[str, Any]) -> str:
    tol = st.get("impedance_tolerance_pct")
    control = ("not required" if not st.get("impedance_controlled") else
               f"required, +/-{_g(tol)} %" if isinstance(tol, (int, float)) else "required")
    return (f"- Finish: {_flat(st.get('finish') or 'not specified')}; impedance control: "
            f"{control}")


def _warning_tally(warnings: Sequence[str]) -> str:
    counts: dict[str, int] = {}
    for w in warnings:
        m = ST_CODE.search(w)
        key = m.group(1) if m else "model notes"
        counts[key] = counts.get(key, 0) + 1
    codes = sorted((k for k in counts if k != "model notes"),
                   key=lambda c: (c[3], int(c[4:])))
    parts = [f"{c} x{counts[c]}" for c in codes]
    if "model notes" in counts:
        parts.append(f"model notes x{counts['model notes']}")
    return f" ({', '.join(parts)})" if parts else ""


def _resolver_line(inp: _Inputs) -> str:
    warnings, errors = _strings(inp.physics.get("warnings")), _strings(inp.physics.get("errors"))
    fd = _mapping(inp.physics.get("fd"))
    mode = _flat(fd.get("mode") or "not recorded")
    if fd.get("backend"):
        mode += (f" ({_flat(fd['backend'])}: {_g(fd.get('computed'))} computed, "
                 f"{_g(fd.get('reused'))} reused, {_g(fd.get('spent_s'))} s)")
    return (f"- Resolver: {len(warnings)} warning(s){_warning_tally(warnings)}, {len(errors)} "
            f"error(s); FD {mode}")


# --- the stackup: the layer table --------------------------------------------------------------
def _copper_row(c: Mapping[str, Any], inp: _Inputs) -> list[str]:
    net = _cell(c.get("net")) if c.get("net") else "-"
    if c.get("net") and c.get("net_source") == "default":
        ident = inp.decision_id(f"planes.{_flat(c.get('name'))}")
        net += f" (default, {_flat(ident)})" if ident else " (default)"
    thickness = _g(c.get("thickness")) + (f" ({_g(c['oz'])} oz)" if c.get("oz") is not None
                                          else "")
    return [_cell(c.get("name")), "copper", _cell(c.get("role")), net, thickness, "-", "-"]


def _ply_rows(d: Mapping[str, Any]) -> list[list[str]]:
    plies, out = _dicts(d.get("plies")), []
    kind = _flat(d.get("kind") or "dielectric") + (" (locked)" if d.get("locked") else "")
    for j, p in enumerate(plies, 1):
        name = _flat(d.get("name")) + (f" ({j}/{len(plies)})" if len(plies) > 1 else "")
        material = " ".join(x for x in (_flat(p.get("label") or p.get("material_id") or ""),
                                        _flat(p.get("construction") or "")) if x)
        dk = f"{_g(p.get('er'))} ({_flat(p.get('er_basis') or '?')} @ {_ghz(p.get('er_freq_hz'))})"
        out.append([_cell(name), "dielectric", _cell(kind), _cell(material or "-"),
                    _g(p.get("thickness")), dk, _g(p.get("df"))])
    return out or [[_cell(d.get("name")), "dielectric", _cell(kind), "-", _g(d.get("thickness")),
                    "-", "-"]]


def _mask_row(side: str, mask: Mapping[str, Any]) -> list[str]:
    dk = f"{_g(mask.get('dk'))} ({_flat(mask.get('basis') or '?')})"
    return [f"{side}.Mask", "mask", _cell(mask.get("source")), _cell(mask.get("material") or "lpi"),
            _g(mask.get("over_substrate")), dk, _g(mask.get("df"))]


def _layer_table(inp: _Inputs) -> list[str]:
    st, copper = inp.stackup, inp.copper()
    if not copper:
        return ["- Layer table: not recorded (the stackup section has no copper list)."]
    dielectrics = _dicts(st.get("dielectrics"))
    mask = _mapping(st.get("mask"))
    coated = bool(mask) and mask.get("source") != "none"
    rows = [_mask_row("F", mask)] if coated else []
    for k, c in enumerate(copper):
        rows.append(_copper_row(c, inp))
        if k < len(dielectrics):
            rows += _ply_rows(dielectrics[k])
    if coated:
        rows.append(_mask_row("B", mask))
    return _table(LAYER_HEADER, rows)


def _stackup_section(inp: _Inputs) -> list[str]:
    st = inp.stackup
    if not st:
        errors = _strings(inp.physics.get("errors"))
        why = "; ".join(errors) if errors else "the stackup has ST-E1..E8 errors (see G1 / G2)"
        return ["### Stackup", "", f"- Not resolved: the physical design was not written: {why}"]
    artifact = inp.physics.get("artifact")
    return ["### Stackup", "", _build_line(st), _rationale_line(inp), _thickness_line(st),
            _dk_line(st), _mask_line(st), _finish_line(st),
            f"- Artifact: {_code(artifact) if artifact else 'not written'}", _resolver_line(inp),
            "", *_layer_table(inp)]


# --- impedance ------------------------------------------------------------------------------------
def _net_title(net: str, rec: Mapping[str, Any], inp: _Inputs) -> str:
    spec = _mapping(_mapping(inp.design.get("net_specs")).get(net))
    partner = spec.get("diff_pair")
    return f"{net} (pair {_flat(partner)})" if partner and rec.get("kind") == "diff" else net


def _reference(lay: Mapping[str, Any], inp: _Inputs) -> str:
    nets = {_flat(c.get("name")): c.get("net") for c in inp.copper()}
    ref = _mapping(lay.get("ref"))
    names = [*_strings(ref.get("above")), *_strings(ref.get("below"))]
    return " / ".join(f"{n} {_flat(nets.get(n))}".strip() if nets.get(n) else n
                      for n in names) or "-"


def _status_text(lay: Mapping[str, Any]) -> str:
    status = _flat(lay.get("status") or "not recorded")
    text = STATUS_TEXT.get(status, status)
    if status == "fd":
        fd = _mapping(lay.get("fd"))
        inside = ", ".join(x for x in (_flat(fd.get("solver") or ""),
                                       f"{_flat(fd['grid'])} grid" if fd.get("grid") else "") if x)
        return f"{text} ({inside})" if inside else text
    if status == "closed_form":
        return f"{text} (class {_flat(lay.get('accuracy_class') or '?')}, not FD-verified)"
    return text


def _layer_row(title: str, name: str, rec: Mapping[str, Any], lay: Mapping[str, Any],
               inp: _Inputs) -> list[str]:
    line = _flat(lay.get("line") or rec.get("line") or "-")
    over = _strings(lay.get("dielectric_layers"))
    if over:
        line += f" ({', '.join(over)} as dielectric)"
    target = f"{_g(lay.get('z_target', rec.get('z_target')))} ohm " + \
        TARGET_TEXT.get(_flat(lay.get("target_kind") or ""), _flat(lay.get("target_kind") or ""))
    window = lay.get("width_window")
    width = _g(lay.get("width")) + (f" [{_g(window[0])}, {_g(window[1])}]"
                                   if isinstance(window, (list, tuple)) and len(window) == 2
                                   else "")
    gap = (f"{_g(lay['pair_gap'])} pair" if lay.get("pair_gap") is not None else
           f"{_g(lay['ground_gap'])} to GND" if lay.get("ground_gap") is not None else "-")
    achieved = _g(lay.get("z_achieved")) + (f" ({_g(lay['error_pct'])} %)"
                                            if lay.get("error_pct") is not None else "")
    klass = f"{_flat(lay.get('accuracy_class') or '?')} (est. {_g(lay.get('est_error_pct'))} %)"
    primary = " (primary)" if name == rec.get("primary_layer") else ""
    return [_cell(title), f"{_cell(name)}{primary}", _cell(line), target.strip(), width, gap,
            achieved, _cell(_reference(lay, inp)), klass, _status_text(lay)]


def _issue_text(rec: Mapping[str, Any]) -> str:
    issues = [f"{_flat(i.get('id'))}: {_flat(i.get('message'))}" for i in _dicts(rec.get("issues"))
              if i.get("severity") == "error"] or [_flat(rec.get("status") or "see the G1 issues")]
    return "; ".join(issues)


def _net_rows(net: str, rec: Mapping[str, Any], inp: _Inputs) -> list[list[str]]:
    title = _net_title(net, rec, inp)
    layers = {k: _mapping(v) for k, v in _mapping(rec.get("layers")).items()}
    if not layers:
        kind = TARGET_TEXT["zdiff" if rec.get("kind") == "diff" else "z0"]
        target = f"{_g(rec.get('z_target'))} ohm {kind}"
        return [[_cell(title), "-", "-", target, "-", "-", "-", "-", "-",
                 _cell(f"no geometry ({_issue_text(rec)})")]]
    order = [_flat(c.get("name")) for c in inp.copper()]
    names = sorted(layers, key=lambda n: order.index(n) if n in order else len(order))
    return [_layer_row(title, n, rec, layers[n], inp) for n in names]


def _routed_line(inp: _Inputs) -> list[str]:
    routed = _mapping(inp.dsn.get("routed_layers"))
    if not routed:
        return []
    excluded = _mapping(inp.dsn.get("excluded_layers"))
    parts = []
    for net in sorted(routed):
        text = f"{_flat(net)} {', '.join(_strings(routed[net])) or 'none'}"
        if _strings(excluded.get(net)):
            text += f" (excluded {', '.join(_strings(excluded[net]))})"
        parts.append(text)
    tail = ("" if inp.dsn.get("layer_rule_verified") else
            "; other layers are excluded until per-class layer_rule is verified (D7.9)")
    return [f"- Freerouting layers: {'; '.join(parts)}{tail}"]


def _impedance_section(inp: _Inputs) -> list[str]:
    head = ["### Impedance", ""]
    if not inp.stackup:
        return head + ["- Not resolved (see Stackup)."]
    if not inp.lines:
        return head + ["- No controlled-impedance nets in this design."]
    rows = [r for net in sorted(inp.lines) for r in _net_rows(net, inp.lines[net], inp)]
    lines = head + _table(LINE_HEADER, rows) + _routed_line(inp)
    lines += [f"- Decision ({_flat(d.get('confidence'))}): {_flat(d.get('decision'))}"
              for d in inp.decisions
              if str(d.get("override") or "").startswith("net_specs.")]
    return lines


# --- design rules ---------------------------------------------------------------------------------
def _canary_text(res: Mapping[str, Any]) -> str:
    types = ", ".join(f"{k} x{v}" for k, v in _mapping(res.get("types")).items()) or "no hit"
    if res.get("ok"):
        return f"compiled (canary fired: {types}, rc {_g(res.get('rc'))})"
    reason = _flat(res.get("reason") or "did not compile")
    detail = f" (rc {_g(res.get('rc'))}" + (", crashed)" if res.get("crash") else ")")
    return f"did NOT compile on {_code(res.get('kicad_cli'))}: {reason}{detail}"


def _rules_section(inp: _Inputs) -> list[str]:
    head = ["### Design rules", ""]
    dru, rules = inp.dru, _mapping(inp.physics.get("rules"))
    if not inp.physics:
        return head + ["- Not produced: the run stopped before the physical-design stage."]
    if not dru:
        return head + ["- Managed rules: not checked (the run stopped before the canary)"]
    families = _mapping(dru.get("families") or rules.get("families"))
    counted = ", ".join(f"{k} x{v}" for k, v in families.items())
    count = dru.get("rules", rules.get("rules"))
    sha = _flat(dru.get("sha256") or rules.get("sha256") or "")[:SHA_SHOWN]
    inside = f"{_g(count)} rules" + (f": {counted}" if counted else "") + \
        (f"; sha256 {sha}" if sha else "")
    status = (f"compiled on {_code(dru.get('kicad_cli'))} " + _canary_text(dru)[len("compiled "):]
              if dru.get("ok") else _canary_text(dru))
    lines = head + [f"- Managed rules: {_code(dru.get('path') or rules.get('path'))} ({inside}): "
                    f"{status}"]
    return lines + [f"- Also checked on {_code(o.get('kicad_cli'))}: {_canary_text(o)}"
                    for o in _dicts(dru.get("others"))]


# --- layer-role checks ----------------------------------------------------------------------------
def _unjudged(roles: Mapping[str, Any]) -> list[str]:
    if roles.get("skipped"):
        return [f"- Layer-role checks: skipped: {_flat(roles['skipped'])}"]
    why = _flat(roles.get("error") or roles.get("reason") or "no reason recorded")
    needs = _strings(roles.get("needs"))
    return [f"- Layer-role checks: not judged: {why}"
            + (f"; needs: {'; '.join(needs)}" if needs else "")]


def _r8_lines(roles: Mapping[str, Any]) -> list[str]:
    m = _mapping(_mapping(roles.get("metrics")).get("plane_layers"))
    planes = _mapping(m.get("plane_layers"))
    named = ", ".join(f"{k} {_flat(v)}" for k, v in planes.items()) or "none"
    issues = _strings(roles.get("issues"))
    return [f"- R8 foreign copper on plane layers: {len(issues)} issue(s); planes {named}; "
            f"foreign tracks {_g(m.get('foreign_tracks'))} ({_g(m.get('foreign_length_mm'))} mm),"
            f" foreign zones {_g(m.get('foreign_zones'))}",
            *(f"  - {i}" for i in issues[:LISTED]),
            *([f"  - and {len(issues) - LISTED} more"] if len(issues) > LISTED else [])]


def _warnings_of(roles: Mapping[str, Any], rule: str) -> list[str]:
    return [w for w in _strings(roles.get("warnings")) if w.startswith(f"{rule}:")]


def _r27_r28_lines(roles: Mapping[str, Any]) -> list[str]:
    metrics = _mapping(roles.get("metrics"))
    t, d = _mapping(metrics.get("ref_transitions")), _mapping(metrics.get("dual_stripline"))
    r27, r28 = _warnings_of(roles, "R27"), _warnings_of(roles, "R28")
    return [f"- R27 reference transitions: {len(r27)} warning(s); impedance vias "
            f"{_g(t.get('impedance_vias'))}, reference changes {_g(t.get('reference_changes'))}, "
            f"without a return path {_g(t.get('unsupported'))}",
            *(f"  - {w}" for w in r27[:LISTED]),
            f"- R28 dual-stripline parallel runs: {len(r28)} warning(s); "
            f"{_g(d.get('dual_stripline_lines'))} line(s) checked",
            *(f"  - {w}" for w in r28[:LISTED])]


def _judged_by(roles: Mapping[str, Any]) -> str:
    st = _mapping(roles.get("stackup"))
    board, stack = _strings(roles.get("board_copper")), _strings(roles.get("stackup_copper"))
    copper = ("board copper = stackup copper" if board == stack else
              f"board copper {', '.join(board)} differs from the stackup copper "
              f"{', '.join(stack)}")
    note = f"; {_flat(st['note'])}" if st.get("note") else ""
    return (f"- Judged by pcbnew {_flat(roles.get('pcbnew') or '?')} on the "
            f"{_flat(st.get('source') or 'resolved')} stackup; {copper}{note}")


def _roles_section(inp: _Inputs) -> list[str]:
    head, roles = ["### Layer-role checks (G4)", ""], inp.roles
    if not roles:
        why = ("not recorded for this run" if "G4" in inp.gates_ran else
               "not reached (the run stopped before G4)")
        return head + [f"- Layer-role checks (R8 / R27 / R28): {why}."]
    if not roles.get("judged"):
        return head + _unjudged(roles)
    return head + _r8_lines(roles) + _r27_r28_lines(roles) + [_judged_by(roles)]


# --- fab outputs ----------------------------------------------------------------------------------
def _job_line(job: Mapping[str, Any]) -> str:
    if not job.get("path"):
        return "- Job file: not written (see the problems below)"
    rows, expected = job.get("rows"), job.get("expected_rows")
    notes = _strings(job.get("notes"))
    check = (f"expected {_g(expected)}" if isinstance(expected, int) else
             f"not checked: {'; '.join(notes)}" if notes else "not checked")
    return f"- Job file: {_code(_basename(job['path']))}: {_g(rows)} thickness rows ({check})"


def _fab_lines(fab: Mapping[str, Any]) -> list[str]:
    ipc = _mapping(fab.get("ipc2581"))
    ipc_text = (f"{_code(_basename(ipc.get('path')))} ({'; '.join(_strings(ipc.get('reasons')))})"
                if ipc.get("path") else "not needed (2 copper layers, FR-4 only, no impedance "
                                        "control)")
    problems = _strings(fab.get("problems"))
    zipped = _code(fab["zip"]) if fab.get("zip") else "none"
    tally = f"{len(problems)} (G5 blockers)" if problems else "none"
    return [_job_line(_mapping(fab.get("gbrjob"))), f"- IPC-2581: {ipc_text}",
            f"- Stackup CSV: {_code(_basename(fab.get('csv')))}; fab notes: "
            f"{_code(_basename(fab.get('notes')))}",
            f"- Zip: {zipped}; problems: {tally}", *(f"  - {p}" for p in problems)]


def _fab_section(inp: _Inputs) -> list[str]:
    head = ["### Fab outputs (G5)", ""]
    if not inp.fab:
        return head + ["- Not produced: the run stopped before G5."]
    fab = _mapping(inp.fab.get("stackup"))
    if not fab:
        return head + ["- Fab stackup outputs: not recorded for this run (the export predates "
                       "the D7 stage or ran without the d7 feature)."]
    if fab.get("checked") is False:
        return head + [f"- Fab stackup outputs: not checked: "
                       f"{'; '.join(_strings(fab.get('problems'))) or 'no reason recorded'}"]
    return head + _fab_lines(fab)


# --- the declaration (runs that never reached the stage) ---------------------------------------
def _declared(design: Mapping[str, Any]) -> str:
    st, count = design.get("stackup"), design.get("copper_layers")
    specs = _mapping(design.get("net_specs"))
    nets = sorted(n for n, s in specs.items() if isinstance(s, Mapping)
                  and any(s.get(k) is not None for k in ("impedance", "diff", "freq_hz")))
    parts = []
    if isinstance(st, Mapping) and {"h", "er"} & set(st):
        parts.append(f"legacy stackup shorthand h {_g(st.get('h'))} mm, Er {_g(st.get('er'))}")
    elif isinstance(st, Mapping) and st.get("preset"):
        parts.append(f"stackup preset {_code(st['preset'])}")
    elif isinstance(st, Mapping) and isinstance(st.get("layers"), list):
        coppers = sum(1 for lay in _dicts(st["layers"]) if lay.get("type") == "copper")
        parts.append(f"a declared stackup of {coppers} copper layers")
    if count is not None:
        parts.append(f"copper_layers {_g(count)}")
    if isinstance(st, Mapping) and _mapping(st.get("impedance_control")).get("required"):
        parts.append("impedance control required")
    if nets:
        parts.append(f"{len(nets)} impedance net(s): {', '.join(nets)}")
    text = ", ".join(parts) or ("no stackup and no copper_layers (the ADR D7.2.4 4-layer "
                                "default applies)")
    return f"- Declared in the netlist: {text}"


# --- the hooks ---------------------------------------------------------------------------------
def render(review: Any) -> list[str]:
    """The section body (Markdown lines without the '## Stackup and impedance' heading):
    Stackup, Impedance, Design rules, Layer-role checks (G4), Fab outputs (G5); one line
    plus the netlist's declaration when the run never resolved its physical design."""
    inp = _Inputs(review)
    if not inp.physics:
        first = NOT_RECORDED if inp.built() else NOT_PRODUCED
        return [first, _declared(inp.design)]
    lines = _stackup_section(inp) + [""] + _impedance_section(inp) + [""]
    lines += _rules_section(inp) + [""] + _roles_section(inp) + [""] + _fab_section(inp)
    return [_flat_line(ln) for ln in lines]


def _flat_line(line: str) -> str:
    return line.replace("\r", " ").replace("\n", " ")


def _row_item(net: str, layer: str, lay: Mapping[str, Any]) -> str | None:
    status, line = lay.get("status"), _flat(lay.get("line") or "line")
    width, klass = _g(lay.get("width")), _flat(lay.get("accuracy_class") or "?")
    err = _g(lay.get("est_error_pct"))
    if status == "fallback_unverified":
        return (f"Confirm the impedance of {net} on {layer} with the fab's field solver: {line} "
                f"width {width} mm is a class {klass} fallback (est. error {err} %), not "
                "FD-verified (install numpy + scipy and re-run the resolver, or accept the "
                "recorded G3 decision)")
    if status == "closed_form":
        return (f"{net} on {layer}: the {line} width {width} mm is a closed-form result (class "
                f"{klass}, est. error {err} %) that was not FD-verified")
    return None


def _line_items(inp: _Inputs) -> list[str]:
    items: list[str] = []
    order = [_flat(c.get("name")) for c in inp.copper()]
    for net in sorted(inp.lines):
        rec = inp.lines[net]
        layers = {k: _mapping(v) for k, v in _mapping(rec.get("layers")).items()}
        if not layers and rec.get("status") == "error":
            items.append(f"No impedance geometry for {net}: {_issue_text(rec)}")
        for name in sorted(layers, key=lambda n: order.index(n) if n in order else len(order)):
            item = _row_item(net, name, layers[name])
            if item:
                items.append(item)
    return items


def open_items(review: Any) -> list[str]:
    """The open items of this section: every fallback_unverified / closed_form impedance row,
    nets without geometry, a mask declared null, and the napkin layer-count default."""
    inp = _Inputs(review)
    items = _line_items(inp)
    mask = _mapping(inp.stackup.get("mask"))
    if inp.lines and mask.get("source") == "none" and inp.stackup.get("source") != "legacy":
        items.append("Open the solder mask over the impedance lines listed in the Impedance "
                     "table: stackup.mask is null (ST-W9), the lines were computed without mask")
    if inp.policy.get("applied"):
        ident = inp.policy.get("decision") or inp.decision_id("stackup.preset")
        count = _g(inp.policy.get("copper_layers"))
        items.append(f"Confirm the napkin layer-count default: {count} layers, preset "
                     f"{_flat(inp.policy.get('preset'))}"
                     + (f" (decision {_flat(ident)})" if ident else "")
                     + "; declare copper_layers or a stackup with a rationale to override it")
    return [_flat(i) for i in items]
