"""The D7 stackup docs and agent prompts (D7-30a) checked against the code, never by
self-attestation.

docs/STACKUP.md (the reference), skills/netlist-pipeline/stackup.md and
skills/pcb-design/stackup.md (the working summaries), the layer-roles sections of
skills/agent-routing/SKILL.md and agents/router.md, and the stackup-outputs section of
agents/fab-checker.md. Checked here:

  * every D7 netlist key (netlist_schema_d7.D7_KEYS) is in the reference, as a JSON key
    or a code span;
  * every ST-* code of lint_rules_stackup.CODES is in the reference's lint table with the
    level the code gives it, every ST-E* row has a remedy, and no doc names a code the
    lint does not emit;
  * every physics_tools tool is called in the reference and the netlist summary, and
    every call written in a code span or fence names a served MCP tool (real parameters)
    or a plugin function;
  * the JSON stackup examples resolve through stackup_model without an ST-E error;
  * the rule ids, DRU families and prefixes, markers, canary, artifact suffixes, fab
    output suffixes, FD budget, DSN class prefixes, harness constants, report keys and
    hooks match their modules;
  * the prompts' named sections carry the facts their readers act on (the artifact, R8 /
    R6 / R7 through route_check, locked preroutes, the wave-3 API guards; the job-file
    rows, the IPC-2581 rule, the notes and the CSV);
  * relative links, ${CLAUDE_PLUGIN_ROOT} paths and /kicad-eda: commands exist.
"""
from __future__ import annotations

import importlib
import inspect
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import pytest

import board_physics
import decisions
import drc_guard
import dru_writer
import dsn_layers
import emc_stitch
import fab_stackup
import fieldsolve2d
import kicad_server
import layer_policy
import layer_role_checks
import lint_rules_stackup
import netlist_schema_d7 as d7
import physics_resolve
import physics_tools
import rp_feature_d7
import rp_hooks
import rp_route_modes
import stackup_model
import stages_d7

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "STACKUP.md"
NETLIST_REF = ROOT / "skills" / "netlist-pipeline" / "stackup.md"
PCB_REF = ROOT / "skills" / "pcb-design" / "stackup.md"
ROUTING_SKILL = ROOT / "skills" / "agent-routing" / "SKILL.md"
ROUTER = ROOT / "agents" / "router.md"
FAB_CHECKER = ROOT / "agents" / "fab-checker.md"
DOCS = (GUIDE, NETLIST_REF, PCB_REF)
PROMPTS = (ROUTING_SKILL, ROUTER, FAB_CHECKER)
MODULE_DIRS = (ROOT / "server", ROOT / "server" / "tools", ROOT / "pipeline")
MATH_NAMES = frozenset({"abs", "max", "min", "sqrt"})
# the ADR D7.8 rules enforced in wave 2a (the RF families R9-R19, R21, R25 are 2b)
WAVE_2A_RULES = ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R20", "R22", "R23", "R24",
                 "R26", "R27", "R28")
IMPEDANCE_KEYS = ("impedance", "diff", "diff_pair", "gap", "layers", "line", "width",
                  "ground_gap", "freq_hz", "z_tol_pct", "ref_nets", "ref_layers", "route")
DECLARATION_WORDS = ("copper_layers", "rationale", "preset", "layers", "roles", "mixed", "mask",
                     "hybrid", "check_layer_count")
_FENCE = re.compile(r"^```([^\n`]*)\n(.*?)^```", re.M | re.S)
_SPAN = re.compile(r"`([^`\n]+)`")
_CALL = re.compile(r"(?<![\w.$/<-])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\(")
_KEYWORD = re.compile(r"^([A-Za-z_]\w*)\s*=(?!=)\s*(.*)$", re.S)
_JSON_KEY = re.compile(r'"([A-Za-z_]\w*)"\s*:')
_IDENT = re.compile(r"[A-Za-z_]\w*")
_ST_CODE = re.compile(r"\b(?:ST-[EW]\d+|IMP-E\d+)\b")
_RULE_ID = re.compile(r"\bR\d{1,2}\b")
_LINK = re.compile(r"\]\(([^)\s]+)\)")
_PLUGIN_PATH = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./-]+)")
_COMMAND = re.compile(r"/kicad-eda:([a-z][\w-]*)")
_REPORT_KEY = re.compile(r"report\[(?:'|\")(\w+)(?:'|\")\]|\breport key `(\w+)`")
_HOOK = re.compile(r"`(\w+)`\s+hook|\bhooks?\s+`(\w+)`")


# --- reading the docs ------------------------------------------------------------------
@lru_cache(maxsize=None)
def _text(path: Path) -> str:
    assert path.is_file(), f"{path.relative_to(ROOT)} is missing"
    return path.read_text(encoding="utf-8")


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _snippets(path: Path) -> list[tuple[str, str]]:
    """(language, code) of every fence and every inline code span outside them."""
    text = _text(path)
    fences = [(m.group(1).strip().lower(), m.group(2)) for m in _FENCE.finditer(text)]
    return fences + [("", m.group(1)) for m in _SPAN.finditer(_FENCE.sub("", text))]


def _section(path: Path, heading: str) -> str:
    """The body under the first '## ' heading containing `heading` (case-insensitive)."""
    text = _text(path)
    m = re.search(rf"^## [^\n]*{re.escape(heading)}[^\n]*$", text, re.M | re.I)
    assert m, f"{_rel(path)} has no '## ...{heading}...' section"
    nxt = re.search(r"^## ", text[m.end():], re.M)
    return text[m.end():m.end() + nxt.start()] if nxt else text[m.end():]


def _table(text: str, header: str) -> list[list[str]]:
    """The body rows (cells) of the first Markdown table whose header row contains `header`."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.lstrip().startswith("|") and header in line), None)
    assert start is not None, f"no table with a {header!r} header"
    rows = []
    for line in lines[start + 2:]:
        if not line.lstrip().startswith("|"):
            break
        rows.append([c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]])
    return rows


def _split_args(text: str) -> list[str]:
    """The top-level arguments of the call whose '(' precedes `text`."""
    args, depth, quote, start = [], 0, "", 0
    for i, ch in enumerate(text):
        if quote or ch in "\"'":
            quote = "" if ch == quote else quote or ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}" and depth == 0:
            return [a for a in (*args, text[start:i].strip()) if a]
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            args, start = [*args, text[start:i].strip()], i + 1
    return [*args, "<unbalanced>"]


def _calls(path: Path) -> list[tuple[str, list[str]]]:
    return [(m.group(1), _split_args(code[m.end():]))
            for _lang, code in _snippets(path) for m in _CALL.finditer(code)]


def _code_tokens(path: Path) -> set[str]:
    """Identifiers written as code: JSON keys of the fences and the words of the spans."""
    found: set[str] = set()
    for lang, code in _snippets(path):
        found |= set(_JSON_KEY.findall(code))
        if not lang:
            found |= set(_IDENT.findall(code))
    return found


@lru_cache(maxsize=None)
def _served() -> dict[str, Callable[..., Any]]:
    return {fn.__name__: fn for mod in kicad_server.TOOL_MODULES for fn in mod.TOOLS}


def _signature_problems(name: str, args: list[str], fn: Callable[..., Any]) -> list[str]:
    """Keywords the tool lacks, more positionals than it takes, an unclosed call."""
    params = inspect.signature(fn).parameters
    plain = [a for a in args if a != "..." and not _KEYWORD.match(a)]
    return ([f"{name}(): no parameter {m.group(1)!r}" for a in args
             if (m := _KEYWORD.match(a)) and m.group(1) not in params]
            + [f"{name}(): too many positional arguments" for _ in plain[len(params):][:1]]
            + [f"{name}(): unbalanced call" for a in args if a == "<unbalanced>"])


def _call_problems(path: Path) -> list[str]:
    problems: list[str] = []
    for name, args in _calls(path):
        head, _, attr = name.partition(".")
        if attr:                                   # dotted: a plugin module's function, or
            if not any((d / f"{head}.py").is_file() for d in MODULE_DIRS):
                continue                           # DRU syntax such as A.intersectsArea(...)
            if not callable(getattr(importlib.import_module(head), attr, None)):
                problems.append(f"{name} is not a function of a plugin module")
        elif name in MATH_NAMES:
            continue
        elif name not in _served():
            problems.append(f"{name}() is not a served MCP tool")
        else:
            problems += _signature_problems(name, args, _served()[name])
    return problems


# --- netlist keys and lint codes ------------------------------------------------------------
def test_every_d7_netlist_key_is_in_the_reference():
    tokens = _code_tokens(GUIDE)
    missing = [k for k in d7.D7_KEYS if k not in tokens]
    assert not missing, f"docs/STACKUP.md never writes the D7 keys {missing}"


def _lint_rows() -> dict[str, list[str]]:
    rows = _table(_section(GUIDE, "lint"), "| Code |")
    return {cells[0].strip("`"): cells for cells in rows if cells}


def test_every_st_code_is_in_the_lint_table_with_its_level_and_a_remedy():
    rows = _lint_rows()
    codes = {c for c in lint_rules_stackup.CODES if c.startswith("ST-")}
    missing = sorted(codes - set(rows))
    assert not missing, f"docs/STACKUP.md does not list {missing}"
    problems = []
    for code, cells in rows.items():
        if code not in lint_rules_stackup.CODES:
            problems.append(f"{code}: lint_rules_stackup never emits it")
            continue
        level, finding, remedy = cells[1].lower(), cells[2], cells[3] if len(cells) > 3 else ""
        want = ("error" if code.endswith(tuple("0123456789")) and "-E" in code else
                "info" if lint_rules_stackup.CODES[code].startswith("info") else "warning")
        if want not in level:
            problems.append(f"{code}: level {cells[1]!r}, the code says {want}")
        if not finding.strip():
            problems.append(f"{code}: no finding")
        if "-E" in code and not remedy.strip():
            problems.append(f"{code}: no remedy")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("path", (*DOCS, *PROMPTS), ids=_rel)
def test_no_doc_names_a_code_the_lint_never_emits(path):
    unknown = sorted(set(_ST_CODE.findall(_text(path))) - set(lint_rules_stackup.CODES)
                     - {"IMP-E1"})                   # impedance's own id (the facade)
    assert not unknown, f"{_rel(path)} names unknown lint codes {unknown}"


# --- tools and calls ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", (*DOCS, FAB_CHECKER), ids=_rel)
def test_every_call_is_a_served_tool_or_plugin_function(path):
    problems = _call_problems(path)
    assert not problems, f"{_rel(path)}:\n" + "\n".join(problems)


@pytest.mark.parametrize("path", (GUIDE, NETLIST_REF), ids=_rel)
def test_every_physics_tool_is_called(path):
    called = {name for name, _ in _calls(path)}
    missing = [fn.__name__ for fn in physics_tools.TOOLS if fn.__name__ not in called]
    assert not missing, f"{_rel(path)} never calls the physics tools {missing}"


def test_tool_table_lists_every_physics_tool_once():
    rows = _table(_section(GUIDE, "physics tools"), "| Tool |")
    heads = [re.match(r"`?([A-Za-z_]\w*)", r[0]).group(1) for r in rows if r]
    assert sorted(heads) == sorted(fn.__name__ for fn in physics_tools.TOOLS), heads


# --- the examples resolve -----------------------------------------------------------------------
def _examples() -> list[dict]:
    found = []
    for lang, code in _snippets(GUIDE):
        if lang != "json":
            continue
        doc = json.loads(code)
        if isinstance(doc, dict) and ("stackup" in doc or "copper_layers" in doc):
            found.append(doc)
    return found


def test_the_json_stackup_examples_resolve_without_errors():
    examples = _examples()
    assert len(examples) >= 4, "the reference needs the 2L, preset, 6L, hybrid examples"
    problems = []
    for doc in examples:
        design = {"components": [], "nets": {"GND": [], "+3V3": []}, **doc}
        rs, issues = stackup_model.resolve(design)
        errors = [str(i) for i in issues if i.severity == "error"]
        if rs is None or errors:
            problems.append(f"{json.dumps(doc)[:80]}...: {errors}")
    assert not problems, "\n".join(problems)
    counts = sorted(stackup_model.resolve({"components": [], "nets": {}, **d})[0].copper_layers
                    for d in examples)
    assert {1, 2, 4, 6} <= set(counts), counts


# --- constants, rules, families ----------------------------------------------------------------
def test_rules_table_covers_the_wave_2a_rules_and_the_dru_families():
    rows = _table(_section(GUIDE, "rules"), "| Rule |")
    ids = {cells[0].strip("`") for cells in rows if cells}
    assert set(WAVE_2A_RULES) <= ids, sorted(set(WAVE_2A_RULES) - ids)
    assert set(dru_writer.FAMILIES) <= ids
    text = _text(GUIDE)
    for family, prefix in dru_writer.FAMILIES.items():
        assert f"`{prefix}" in text, f"the {family} prefix {prefix} is not documented"
    for rule in ("R8:", "R27:", "R28:"):             # the G4 role checks' message prefixes
        assert rule in inspect.getsource(layer_role_checks)
    stray = sorted(set(_RULE_ID.findall(text)) - set(WAVE_2A_RULES)
                   - {f"R{n}" for n in range(9, 20)} - {"R21", "R25"})
    assert not stray, f"rule ids that are not ADR D7.8 rules: {stray}"


def test_managed_rules_facts_match_dru_writer_and_drc_guard():
    text = _text(GUIDE)
    for fact in (dru_writer.BEGIN_MARKER, dru_writer.END_MARKER, drc_guard.CANARY_NET,
                 "track_width (min 10mm)", dru_writer.CANARY_NAME, "(last matching rule",
                 "A./B.-qualified", "disallow zone", "deg"):
        assert fact in text, f"docs/STACKUP.md does not state {fact!r}"
    assert "(min 10mm)" in drc_guard.CANARY_RULE and dru_writer.CANARY_NAME in drc_guard.CANARY_RULE
    assert f"<board stem>{dru_writer.DRU_SUFFIX}" in text
    assert drc_guard.RULES_NOT_COMPILED in text


def test_artifact_and_fab_output_names_match_the_code():
    text = _text(GUIDE)
    assert f"<netlist stem>{physics_resolve.ARTIFACT_SUFFIX}" in text
    assert f"<board stem>{physics_resolve.ARTIFACT_SUFFIX}" in text
    assert physics_resolve.SCHEMA in text and board_physics.ENV_VAR in text
    for suffix in (fab_stackup.GBRJOB_SUFFIX, fab_stackup.CSV_SUFFIX, fab_stackup.NOTES_SUFFIX,
                   fab_stackup.IPC2581_SUFFIX):
        assert f"<stem>{suffix}" in text, suffix
        assert f"<stem>{suffix}" in _section(FAB_CHECKER, "stackup outputs"), suffix
    columns = ", ".join(f"`{c}`" for c in fab_stackup.CSV_COLUMNS)
    assert columns in text, "the CSV columns are not listed as the code writes them"
    for name in physics_resolve.SECTION_BUILDERS:
        assert f"`{name[0]}`" in text, f"artifact section {name[0]} is not documented"


def test_fd_dsn_and_policy_facts_match_the_code():
    text = _text(GUIDE)
    assert fieldsolve2d.BUDGET_ENV in text
    assert f"default {fieldsolve2d.DEFAULT_BUDGET_S:g} s" in text
    assert all(f"`{m}`" in text for m in physics_resolve.FD_MODES), physics_resolve.FD_MODES
    for fact in (dsn_layers.PLANE_CLASS_PREFIX, dsn_layers.NET_CLASS_PREFIX,
                 dsn_layers.RESTRICTED, dsn_layers.UNRESTRICTED, "pad_layer_excluded",
                 "layer_rule", emc_stitch.STACKUP_FLAG):
        assert fact in text, fact
    assert dsn_layers.LAYER_RULE_VERIFIED is True and "verified" in text
    assert str(layer_policy.DEFAULT_COUNT) in text
    for count, table in layer_policy.NAPKIN_PRESETS["jlcpcb"].items():
        for name in set(table.values()):
            assert f"jlcpcb:{name}" in text, f"napkin preset {name} ({count}L) is not named"
    assert decisions.STAGES[0] == "requirements" and '`requirements`' in text
    assert "`stackup.preset`" in text                # the napkin decision's override
    floors = set(re.findall(r"\bF[0-3]\b", _section(GUIDE, "layer count")))
    assert floors == {"F0", "F1", "F2", "F3"}, floors


def test_harness_facts_match_rp_feature_d7_and_stages_d7():
    text = _text(GUIDE)
    assert f"ORDER {rp_feature_d7.ORDER}" in text
    keys = {rp_feature_d7.LAYER_POLICY_KEY, rp_feature_d7.PHYSICS_KEY, rp_feature_d7.DRU_KEY,
            rp_feature_d7.ROLES_KEY, rp_feature_d7.DSN_KEY, rp_feature_d7.FAB_KEY}
    named = {a or b for a, b in _REPORT_KEY.findall(text)}
    assert keys <= named, f"report keys not documented: {sorted(keys - named)}"
    assert f"`{rp_feature_d7.HASH_KEY}`" in text, "the route-hash key is not named"
    hooks = {a or b for a, b in _HOOK.findall(text)}
    defined = {h for h in rp_hooks.HOOKS if callable(getattr(rp_feature_d7, h, None))}
    assert defined <= hooks, f"hooks not documented: {sorted(defined - hooks)}"
    assert not hooks - set(rp_hooks.HOOKS), f"hooks rp_hooks lacks: {hooks - set(rp_hooks.HOOKS)}"
    for fact in (stages_d7.VETO_REASON, stages_d7.K11_BLOCKER, stages_d7.K11_NOTE,
                 rp_route_modes.REFUSED, "PIPELINE_NO_CACHE=1", "<stem>.decisions.jsonl"):
        assert fact in text, f"docs/STACKUP.md does not state {fact!r}"
    assert rp_feature_d7.ROUTE_HASH_DESIGN_KEYS == ("rf", "layer_policy")
    assert "`clock_hz`" in text and "`rf`" in text and "`layer_policy`" in text


def test_role_check_defaults_are_stated():
    text = _text(GUIDE)
    assert f"{layer_role_checks.MAX_DIST_MM:g} mm" in text
    assert f"{layer_role_checks.MIN_LEN_MM:g} mm" in text
    assert f"{layer_role_checks.MAX_ANGLE_DEG:g}" in text


# --- the summaries and the prompts ---------------------------------------------------------------
@pytest.mark.parametrize("path", (NETLIST_REF, PCB_REF), ids=_rel)
def test_summaries_declare_the_count_with_a_rationale(path):
    text = _text(path)
    missing = [w for w in DECLARATION_WORDS if w not in text]
    assert not missing, f"{_rel(path)} lacks {missing}"
    assert "docs/STACKUP.md" in text
    codes = set(_ST_CODE.findall(text))
    assert {"ST-E7", "ST-W12"} <= codes, f"{_rel(path)} names {sorted(codes)}"


def test_netlist_summary_names_the_impedance_keys():
    tokens = _code_tokens(NETLIST_REF)
    missing = [k for k in IMPEDANCE_KEYS if k not in tokens]
    assert not missing, f"netlist-pipeline/stackup.md lacks the net_specs keys {missing}"


@pytest.mark.parametrize("path", (ROUTING_SKILL, ROUTER), ids=_rel)
def test_routing_prompts_read_the_artifact_and_the_managed_rules(path):
    section = _section(path, "layer roles")
    facts = [f"<stem>{physics_resolve.ARTIFACT_SUFFIX}", "R8", "R6", "R7", "`route_check`",
             "locked", "wave 3", dru_writer.DRU_SUFFIX, "`plane`"]
    missing = [f for f in facts if f not in section]
    assert not missing, f"{_rel(path)} layer-roles section lacks {missing}"
    assert "set_enabled_layers" in section or "never" in section


def test_fab_checker_reads_the_stackup_outputs():
    section = _section(FAB_CHECKER, "stackup outputs")
    for fact in ("copper + ", "2 masks", "more than 2 copper", "non-FR-4", "impedance control",
                 "export_fab_package(", "FileFunction", "stackup_json"):
        assert fact in section, f"fab-checker.md stackup-outputs section lacks {fact!r}"
    assert "fab_stackup_check" in inspect.getsource(stages_d7)
    design = {"components": [], "nets": {"GND": [], "+3V3": []},
              "stackup": {"preset": "jlcpcb:JLC06161H-2116",
                          "impedance_control": {"required": True}, "rationale": "6L"}}
    rs, _ = stackup_model.resolve(design)
    reasons = fab_stackup.ipc2581_reasons(rs)
    assert any("more than 2" in r for r in reasons) and any("impedance" in r for r in reasons)
    assert len(fab_stackup.expected_thickness_rows(rs)) == 16 and "16" in section


# --- links, paths, commands ----------------------------------------------------------------------
@pytest.mark.parametrize("path", (*DOCS, *PROMPTS), ids=_rel)
def test_links_plugin_paths_and_commands_resolve(path):
    text = _text(path)
    problems = [f"link {t}" for t in _LINK.findall(text)
                if not re.match(r"^(https?:|mailto:|#)", t)
                and not (path.parent / t.split("#")[0]).exists()]
    problems += [p for p in _PLUGIN_PATH.findall(text) if not (ROOT / p.rstrip(".")).exists()]
    problems += [f"/kicad-eda:{c}" for c in _COMMAND.findall(text)
                 if not (ROOT / "commands" / f"{c}.md").is_file()
                 and not (ROOT / "skills" / c / "SKILL.md").is_file()]
    assert not problems, f"{_rel(path)}: {problems}"


def test_reference_links_the_roadmap_and_the_summaries_link_the_reference():
    assert "ROADMAP_DECISIONS.md" in _text(GUIDE) and "PIPELINE.md" in _text(GUIDE)
    for path in (NETLIST_REF, PCB_REF):
        assert "${CLAUDE_PLUGIN_ROOT}/docs/STACKUP.md" in _text(path), _rel(path)
