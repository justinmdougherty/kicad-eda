"""Prompt contracts checked against the code. The napkin flow (F5-18): commands/napkin.md,
skills/napkin-mode/SKILL.md and agents/placement-reviewer-headless.md. The flows around it
(F5-19): commands/pipeline.md and new-board.md, agents/router.md and the netlist-pipeline,
pcb-design and agent-routing skills. Frontmatter (stdlib parser); every `name(...)` is a served
MCP tool (real parameters) or a plugin function; every run_pipeline `--flag` passes
rp_cli.parse_cli; every subagent has an agents/ file; the napkin skill names every exit code, D5
hard stop (docs/ROADMAP_DECISIONS.md), gates.py fix type / policy / designer task, artifact and
library / physics / review tool; the pipeline prompts name the harness flags, exit codes and
artifacts; the routing prompts describe the automatic pre-KiCad-11 snapshot and route_check's
stall verdict; the three flow skills' descriptions trigger on disjoint wording."""
from __future__ import annotations

import ast
import inspect
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import pytest

import decisions
import gates
import kicad_server
import library_tools
import napkin_policy as npol
import netlist_schema
import physics_tools
import review_report
import review_tools
import rp_cli
import rp_context
import rp_napkin
import rp_route_modes
import safety_screen
import toolchain as tc

ROOT = Path(__file__).resolve().parents[1]
COMMAND = ROOT / "commands" / "napkin.md"
SKILL = ROOT / "skills" / "napkin-mode" / "SKILL.md"
REVIEWER = ROOT / "agents" / "placement-reviewer-headless.md"
PROMPTS = (COMMAND, SKILL, REVIEWER)
PIPELINE_CMD = ROOT / "commands" / "pipeline.md"
NEW_BOARD = ROOT / "commands" / "new-board.md"
ROUTER = ROOT / "agents" / "router.md"
NETLIST_SKILL = ROOT / "skills" / "netlist-pipeline" / "SKILL.md"
PCB_SKILL = ROOT / "skills" / "pcb-design" / "SKILL.md"
ROUTING_SKILL = ROOT / "skills" / "agent-routing" / "SKILL.md"
FLOW_PROMPTS = (PIPELINE_CMD, NEW_BOARD, ROUTER, NETLIST_SKILL, PCB_SKILL, ROUTING_SKILL)
ALL_PROMPTS = (*PROMPTS, *FLOW_PROMPTS)
GATES_SRC = ROOT / "server" / "tools" / "gates.py"
LINT_CORE = ROOT / "server" / "tools" / "lint_rules_core.py"
ROADMAP = ROOT / "docs" / "ROADMAP_DECISIONS.md"
MODULE_DIRS = (ROOT / "server", ROOT / "server" / "tools", ROOT / "pipeline")
MAX_LINES = 400                  # the napkin prompts (F5-18)
MAX_PY_LINES = 800               # RULES: every .py file
PCB_SKILL_MAX_LINES = 1700       # RULES: pcb-design/SKILL.md (1690) grows <= 10 lines over the wave
SKILL_KEYS = ("name", "description")
REQUIRED_KEYS: dict[Path, tuple[str, ...]] = {
    COMMAND: ("description",), SKILL: SKILL_KEYS,
    REVIEWER: ("name", "description", "model", "effort", "maxTurns", "disallowedTools"),
    PIPELINE_CMD: ("description",), NEW_BOARD: ("description",),
    ROUTER: ("name", "description", "effort", "maxTurns"),
    NETLIST_SKILL: SKILL_KEYS, PCB_SKILL: SKILL_KEYS, ROUTING_SKILL: SKILL_KEYS}
MATH_NAMES = frozenset({"abs", "max", "min", "sgn", "sqrt"})   # geometry in prose, not tools
HARNESS_FLAGS = frozenset({rp_cli.STRICT_FLAG, *rp_cli.CHOICES})
ARTIFACT_SUFFIXES = (rp_context.REPORT_SUFFIX, review_report.REVIEW_SUFFIX,
                     decisions.DECISIONS_SUFFIX, rp_route_modes.HANDOFF_SUFFIX)
EXIT_WORDS = {rp_cli.EXIT_PASS: "PASS", rp_cli.EXIT_FAIL: "FAIL", rp_cli.EXIT_USAGE: "usage",
              rp_cli.EXIT_HANDOFF: "handoff"}
SKILL_SUBAGENTS = {"parts-selector", "router", "placement-reviewer-headless"}
SKILL_TRIGGERS = ("hands-off", "no questions", "unattended", "napkin sketch", "just build it",
                  "/kicad-eda:napkin", "pcb-design")
DEFAULT_TOPICS = ("power input", "temperature", "quantity", "fab", "layer count", "regulatory",
                  "board size", "sourcing")
REVIEWER_READS = {"check_design_gates", "drc_pcb", "board_overview", "board_pads", "close_board",
                  "render_pcb", "server_status", "list_open_boards"}     # read-only tools
_FENCE = re.compile(r"^```([^\n`]*)\n(.*?)^```", re.M | re.S)
_SPAN = re.compile(r"`([^`\n]+)`")
_CALL = re.compile(r"(?<![\w.$/<-])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\(")
_KEYWORD = re.compile(r"^([A-Za-z_]\w*)\s*=(?!=)\s*(.*)$", re.S)
_FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*)(?:[ =]([a-z]+(?:\|[a-z]+)*))?")
# `name` subagent | subagent `name` | hyphen-name subagent | kicad-eda:name (not /kicad-eda:);
# a plural 'gate-reviewer subagents' is a description, not an agent's name
_SUBAGENT = re.compile(r"`([a-z][a-z0-9-]*)`\s+subagent|\bsubagent\s+`([a-z][a-z0-9-]*)`|"
                       r"\b([a-z]+(?:-[a-z]+)+)\s+subagent\b|(?<![/\w-])kicad-eda:([a-z][\w-]*)")
_COMMAND = re.compile(r"/kicad-eda:([a-z][\w-]*)")
_PLUGIN_PATH = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./-]+)")
_LINK = re.compile(r"\]\(([^)\s]+)\)")
_BLOCK_SCALARS = (">", ">-", "|", "|-")


# --- reading the prompts ------------------------------------------------------------------
def parse_frontmatter(text: str) -> dict[str, str]:
    """The `key: value` header between the leading '---' lines (block scalars folded into
    one line, quotes removed); ValueError for a malformed header."""
    lines = text.splitlines()
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), 0)
    if not lines or lines[0].strip() != "---" or not end:
        raise ValueError("no frontmatter: the file must start with a '---' line and close one")
    meta: dict[str, str] = {}
    key = ""
    for line in (ln for ln in lines[1:end] if ln.strip()):
        if line[0] in " \t" and key:
            meta = {**meta, key: f"{meta[key]} {line.strip()}".strip()}
            continue
        m = re.match(r"^([A-Za-z][\w-]*):(?:\s+(.*))?$", line)
        if not m or m.group(1) in meta:
            raise ValueError(f"not a 'key: value' line or a duplicate key: {line!r}")
        key, value = m.group(1), (m.group(2) or "").strip()
        quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"
        meta = {**meta, key: "" if value in _BLOCK_SCALARS else value[1:-1] if quoted else value}
    return meta


@lru_cache(maxsize=None)
def _text(path: Path) -> str:
    assert path.is_file(), f"{path.relative_to(ROOT)} is missing"
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _snippets(path: Path) -> list[str]:
    """Every fence body and every inline code span outside the fences."""
    text = _text(path)
    return [m.group(2) for m in _FENCE.finditer(text)] + _SPAN.findall(_FENCE.sub("", text))


def _section(path: Path, heading: str) -> str:
    """The body under the first '## ' heading containing `heading` (case-insensitive)."""
    text = _text(path)
    m = re.search(rf"^## [^\n]*{re.escape(heading)}[^\n]*$", text, re.M | re.I)
    assert m, f"{path.relative_to(ROOT)} has no '## ...{heading}...' section"
    nxt = re.search(r"^## ", text[m.end():], re.M)
    return text[m.end():m.end() + nxt.start()] if nxt else text[m.end():]


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
            for code in _snippets(path) for m in _CALL.finditer(code)]


def _keyword_values(calls: list[tuple[str, list[str]]], tool: str, key: str) -> list[str]:
    """The literal `key=` values of the `tool` calls ('' for a call without it)."""
    return [{m.group(1): m.group(2).strip().strip("\"'") for a in args
             if (m := _KEYWORD.match(a))}.get(key, "") for name, args in calls if name == tool]


@lru_cache(maxsize=None)
def _served() -> dict[str, Callable[..., Any]]:
    return {fn.__name__: fn for mod in kicad_server.TOOL_MODULES for fn in mod.TOOLS}


# --- frontmatter and size -------------------------------------------------------------------
def test_frontmatter_parser():
    meta = parse_frontmatter('---\nname: x\ndescription: >-\n  one\n  two\nm: "q: v"\n---\nbody')
    assert meta == {"name": "x", "description": "one two", "m": "q: v"}
    for bad, why in (("# none\n", "no frontmatter"), ("---\nname: x\n", "no frontmatter"),
                     ("---\nname x\n---\n", "not a 'key"), ("---\na: 1\na: 2\n---\n", "dupl")):
        with pytest.raises(ValueError, match=why):
            parse_frontmatter(bad)


def test_frontmatter_keys_identities_and_triggers():
    metas = {p: parse_frontmatter(_text(p)) for p in PROMPTS}
    missing = {p.name: [k for k in REQUIRED_KEYS[p] if not metas[p].get(k)] for p in PROMPTS}
    assert not any(missing.values()), f"missing or empty frontmatter keys: {missing}"
    skill, agent = metas[SKILL], metas[REVIEWER]
    assert skill["name"] == SKILL.parent.name == "napkin-mode"
    assert agent["name"] == REVIEWER.stem
    assert (agent["model"], agent["effort"], agent["maxTurns"]) == ("sonnet", "high", "25")
    assert {"Write", "Edit"} <= {t.strip() for t in agent["disallowedTools"].split(",")}
    lacking = [t for t in SKILL_TRIGGERS if t not in skill["description"].lower()]
    assert not lacking, f"the napkin-mode description lacks {lacking}"
    command = metas[COMMAND]["description"]
    assert "Hands-off concept to Gerbers" in command and "never ask the user" in command
    body = _text(COMMAND).split("---", 2)[2]
    assert "$ARGUMENTS" in body and "napkin-mode" in body


def test_files_stay_under_the_line_limit():
    sizes = {p.name: p.read_text(encoding="utf-8").count("\n") for p in PROMPTS}
    assert all(n < MAX_LINES for n in sizes.values()), sizes
    own = Path(__file__).read_text(encoding="utf-8").count("\n")
    assert own < MAX_PY_LINES, f"{Path(__file__).name} has {own} lines"
    pcb = PCB_SKILL.read_text(encoding="utf-8").count("\n")
    assert pcb <= PCB_SKILL_MAX_LINES, f"skills/pcb-design/SKILL.md grew to {pcb} lines"


# --- tool calls, flags, subagents, links (every prompt) ------------------------------------
def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _signature_problems(name: str, args: list[str], fn: Callable[..., Any]) -> list[str]:
    """Keywords the tool lacks, more positionals than it takes, an unclosed call."""
    params = inspect.signature(fn).parameters
    plain = [a for a in args if a != "..." and not _KEYWORD.match(a)]
    return ([f"{name}(): no parameter {m.group(1)!r}" for a in args
             if (m := _KEYWORD.match(a)) and m.group(1) not in params]
            + [f"{name}(): too many positional arguments" for _ in plain[len(params):][:1]]
            + [f"{name}(): unbalanced call" for a in args if a == "<unbalanced>"])


@pytest.mark.parametrize("path", ALL_PROMPTS, ids=_rel)
def test_every_call_is_a_served_tool_or_plugin_function(path):
    problems: list[str] = []
    for name, args in _calls(path):
        head, _, attr = name.partition(".")      # dotted: a function of a plugin module
        if attr:
            if not (any((d / f"{head}.py").is_file() for d in MODULE_DIRS)
                    and callable(getattr(__import__(head), attr, None))):
                problems.append(f"{name} is not a function of a plugin module")
        elif name in MATH_NAMES:
            continue
        elif name not in _served():
            problems.append(f"{name}() is not a served MCP tool")
        else:
            problems += _signature_problems(name, args, _served()[name])
    assert not problems, f"{_rel(path)}:\n" + "\n".join(problems)
    stages, levels = (_keyword_values(_calls(path), "record_decision", k)
                      for k in ("stage", "confidence"))
    assert stages or path != SKILL, "the napkin skill never shows a record_decision call"
    assert set(stages) - {""} <= set(decisions.STAGES), stages
    assert set(levels) - {""} <= set(decisions.CONFIDENCE_RANK), levels


def _harness_flags(path: Path) -> list[tuple[str, str]]:
    """The run_pipeline options a prompt writes, (flag, value): every --flag of a napkin
    prompt; elsewhere the harness options and any --flag on a line naming run_pipeline
    (the kicad-cli options in the GUI skill are not the harness's)."""
    return [(flag, value) for line in _text(path).splitlines()
            for flag, value in _FLAG.findall(line)
            if path in PROMPTS or flag in HARNESS_FLAGS or "run_pipeline" in line]


@pytest.mark.parametrize("path", ALL_PROMPTS, ids=_rel)
def test_every_run_pipeline_flag_is_accepted_by_parse_cli(path):
    # a valued option is tried with every value written after it ('--route a|b');
    # parse_cli raises UsageError for an unknown option or value
    for flag, value in _harness_flags(path):
        if flag not in rp_cli.CHOICES:
            rp_cli.parse_cli(["netlist.json", flag])
        for v in (value.split("|") if flag in rp_cli.CHOICES and value else []):
            rp_cli.parse_cli(["netlist.json", f"{flag}={v}"])
    found = set(_FLAG.findall(_text(SKILL)))
    missing = [m for m in rp_context.RESUME_MODES if (rp_cli.RESUME_FLAG, m) not in found]
    assert not missing, f"the skill never runs {rp_cli.RESUME_FLAG} {missing}"


def _subagents(path: Path) -> set[str]:
    return {name for groups in _SUBAGENT.findall(_text(path)) for name in groups if name}


@pytest.mark.parametrize("path", ALL_PROMPTS, ids=_rel)
def test_every_subagent_named_has_an_agent_file(path):
    missing = sorted(a for a in _subagents(path) if not (ROOT / "agents" / f"{a}.md").is_file())
    assert not missing, f"{_rel(path)} names subagents with no agents/<name>.md: {missing}"
    assert SKILL_SUBAGENTS <= _subagents(SKILL), _subagents(SKILL)


@pytest.mark.parametrize("path", ALL_PROMPTS, ids=_rel)
def test_commands_plugin_paths_and_links_resolve(path):
    text = _text(path)
    problems = [f"/kicad-eda:{c}" for c in _COMMAND.findall(text)
                if not (ROOT / "commands" / f"{c}.md").is_file()
                and not (ROOT / "skills" / c / "SKILL.md").is_file()]
    problems += [p for p in _PLUGIN_PATH.findall(text) if not (ROOT / p.rstrip(".")).exists()]
    problems += [t for t in _LINK.findall(text) if not re.match(r"^(https?:|mailto:|#)", t)
                 and not (path.parent / t.split("#")[0]).exists()]
    assert not problems, f"{path.name}: {problems}"


# --- the napkin skill: exit codes, hard stops, artifacts, tools, defaults -----------------
def test_skill_names_every_harness_exit_code():
    codes = {v for k, v in vars(rp_cli).items() if k.startswith("EXIT_") and isinstance(v, int)}
    assert codes == set(EXIT_WORDS), "rp_cli has an exit code this test does not know"
    for code, word in EXIT_WORDS.items():
        row = re.search(rf"^\s*\|\s*`{code}`\s*\|(.*)$", _text(SKILL), re.M)
        assert row, f"no '| `{code}` |' row in the exit-code table"
        assert word.lower() in row.group(1).lower(), f"exit {code} row does not say {word}"


def test_skill_names_every_d5_hard_stop_and_the_safety_flag():
    m = re.search(r"Still a hard stop in napkin mode: (.+?)\. ", _flat(_text(ROADMAP)))
    assert m, "docs/ROADMAP_DECISIONS.md D5 no longer lists the hard stops"
    stops = [re.sub(r"\s*\(.*?\)", "", s).strip() for s in m.group(1).split(", ")]
    section = _flat(_section(SKILL, "hard stops")).lower()
    missing = [t for t in (*stops, f"<stem>{rp_napkin.NAPKIN_TAG}.kicad_pcb",
                           safety_screen.HUMAN_REVIEW, *safety_screen.SAFETY_LABELS.values())
               if t.lower() not in section]
    assert not missing, f"the hard-stops section lacks {missing}"


def test_skill_names_the_artifacts_and_the_library_physics_and_review_tools():
    missing = [s for s in ARTIFACT_SUFFIXES if f"<stem>{s}" not in _text(SKILL)]
    assert not missing, f"artifacts not named: {missing}"
    named = {name for name, _ in _calls(SKILL)} | {s.strip() for s in _snippets(SKILL)}
    for mod in (library_tools, physics_tools, review_tools):
        missing = [fn.__name__ for fn in mod.TOOLS if fn.__name__ not in named]
        assert not missing, f"napkin-mode never names {mod.__name__} tools {missing}"
    early = _section(SKILL, "requirements defaults") + _section(SKILL, "flow")
    absent = [fn.__name__ for fn in physics_tools.TOOLS if fn.__name__ not in early]
    assert not absent, f"physics tools missing from the defaults / flow sections: {absent}"


def test_requirements_defaults_have_valid_override_keys():
    rows = [r.strip() for r in _section(SKILL, "requirements defaults").splitlines()
            if r.strip().startswith("|") and not re.fullmatch(r"\|[\s:|-]+", r.strip())]
    text = " ".join(rows).lower()
    assert all(t in text for t in DEFAULT_TOPICS), [t for t in DEFAULT_TOPICS if t not in text]
    keys = [o for r in rows[1:] for o in _SPAN.findall(r.rstrip("|").split("|")[-1])]
    assert keys, "the defaults table has no override column of `netlist.path` values"
    top = set(netlist_schema.SCHEMA["properties"])
    bad = [k for k in keys if re.split(r"[.\[]", k)[0] not in top]
    assert not bad, f"override keys outside the netlist schema: {bad}"


# --- the policy table covers every task the gates emit ------------------------------------
def _template(node: ast.expr) -> str | None:
    """A task text as written, f-string fields kept as '{expr}' ('fix placement: {issue}')."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else
                       "{" + ast.unparse(v.value) + "}" for v in node.values)
    return None


@lru_cache(maxsize=None)
def _gate_tasks() -> tuple[frozenset[str], frozenset[str]]:
    """(fix types, designer-task templates) gates.py can emit, from its AST: a designer
    task is a _task(desc, False, ...) call, one the harness cannot apply itself."""
    tree = ast.parse(GATES_SRC.read_text(encoding="utf-8"))
    types = {v.value for n in ast.walk(tree) if isinstance(n, ast.Dict)
             for k, v in zip(n.keys, n.values, strict=True)
             if isinstance(k, ast.Constant) and k.value == "type" and isinstance(v, ast.Constant)}
    designer = {t for n in ast.walk(tree) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name) and n.func.id == "_task" and len(n.args) > 1
                and isinstance(n.args[1], ast.Constant) and n.args[1].value is False
                and (t := _template(n.args[0]))}
    return frozenset(types | set(npol.NAPKIN_FIX_KINDS) | set(npol.POLICY_FOR)), \
        frozenset(designer)


def test_policy_table_covers_every_fix_type_policy_and_designer_task():
    table = _flat(_section(SKILL, "policy table"))
    types, designer = _gate_tasks()
    assert {"add_decoupling", "reroute", *npol.NAPKIN_FIX_KINDS} <= types
    assert len(designer) >= 10, sorted(designer)
    policies = {v for k, v in vars(npol).items() if k.startswith("POLICY_") and isinstance(v, str)}
    missing = [f"`{t}`" for t in sorted(types | policies | set(npol.POLICY_FOR.values()))
               if f"`{t}`" not in table]
    missing += [t for t in sorted(designer) if _flat(t) not in table]
    codes = re.findall(r"^\s+(E\d)\s", LINT_CORE.read_text(encoding="utf-8"), re.M)
    missing += [c for c in codes if f"`{c}`" not in table]
    assert codes and not missing, "the napkin policy table does not cover:\n" + "\n".join(missing)


_G3_ISSUES = ("J1: mating face points INTO the board interior (unusable in an enclosure)",
              "J2: mating face is 3.5 mm from the board edge it faces (must be flush)",
              "+3V3: plane on In2.Cu is fractured into separate islands, splitting the net",
              "U3: dissipating exposed-pad part has only 1 thermal via(s) under the EP",
              "U4: 5.0 mm tall inside PI with a 4.0 mm ceiling (B side) - move it out")


def _napkin_verdicts() -> list[dict]:
    """Every gate's verdict in napkin mode, from reports that trip every branch."""
    assert all(npol.classify_g3_issue(i) for i in _G3_ISSUES[:4]), "G3 issue texts drifted"
    pins = (("U1", "5", "PA3", "output", None), ("U2", "12", "GND", "passive", "GND"),
            ("U1", "7", "EN", "input", None))   # no_connect, same-name supply, designer
    detail = [{"pin": f"{r}.{p}", "ref": r, "pad": p, "name": n, "type": t, "same_name_net": s}
              for r, p, n, t, s in pins]
    review = {"errors": ["Lib:X not found"],
              "decoupling": {"uncovered": [{"ref": "U1", "rails_without_cap": ["+3V3"]}]},
              "flags": {"floating_pins": [d["pin"] for d in detail], "floating_detail": detail}}
    return [gates.g1_requirements({"errors": ["E3 net 'X' has 1 pad(s)"]}, {}, napkin=True),
            gates.g2_schematic({"ok": False, "erc_violations": 2}, review,
                               {"blockers": ["U5: Tj 150 C"]}, napkin=True),
            gates.g3_placement({"ok": False, "error": "no footprint", "missing_pads": ["U1.99"]},
                               {"courtyard_violations": 1}, list(_G3_ISSUES), napkin=True),
            gates.g4_layout({"unconnected": 3}, {"errors": 1, "types": {"clearance": 1}}, ["w"]),
            gates.g4_layout({"unconnected": None, "error": "timeout"}, {}, []),
            gates.g5_fab({"ok": False, "files": 2}, sourcing={"missing": ["R1"]})]


def test_policy_table_covers_every_task_a_napkin_run_emits():
    table = _flat(_section(SKILL, "policy table"))
    shapes = [".+?".join(map(re.escape, re.split(r"\{[^{}]*\}", t))) for t in _gate_tasks()[1]]
    emitted, problems = set(), []
    for task in (t for v in _napkin_verdicts() for t in v["tasks"]):
        if task["auto"]:
            emitted.add(task["fix"]["type"])
            problems += [f"`{x}`" for x in (task["fix"]["type"], task["policy"])
                         if x and f"`{x}`" not in table]
        elif not any(re.fullmatch(s, task["desc"], re.S) for s in shapes):
            problems.append(f"designer task with no template in gates.py: {task['desc']}")
    assert set(npol.NAPKIN_FIX_KINDS) <= emitted, sorted(set(npol.NAPKIN_FIX_KINDS) - emitted)
    assert not problems, "\n".join(problems)


# --- the headless placement reviewer -------------------------------------------------------
def test_reviewer_is_read_only_and_never_saves_the_board():
    calls = _calls(REVIEWER)
    names = {name for name, _ in calls}
    assert REVIEWER_READS - {"board_pads", "server_status", "list_open_boards"} <= names
    assert names <= REVIEWER_READS, f"not read-only: {sorted(names - REVIEWER_READS)}"
    saves = _keyword_values(calls, "close_board", "save")
    assert saves and set(saves) == {"False"}, f"close_board must pass save=False: {saves}"
    assert "placement" in _keyword_values(calls, "check_design_gates", "stage")
    assert {"top", "bottom"} <= set(_keyword_values(calls, "render_pcb", "side"))


# --- the flows around napkin mode (F5-19) ---------------------------------------------------
FLOW_SKILLS: dict[str, Path] = {"pcb-design": PCB_SKILL, "netlist-pipeline": NETLIST_SKILL,
                                "napkin-mode": SKILL}
FLOW_TRIGGERS: dict[str, tuple[str, ...]] = {      # lower case: the wording only one flow uses
    "pcb-design": ("interactive", "consulted at each stage", "work in the kicad gui"),
    "netlist-pipeline": ("headless", "supervised", "stays in the loop"),
    "napkin-mode": tuple(t for t in SKILL_TRIGGERS if t not in FLOW_SKILLS)}
_OUT_OF_SCOPE = re.compile(r"\b(?:not|never|instead)\b|out of scope", re.I)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")
SNAPSHOT_FILE = f"{tc.SNAPSHOT_TAG}{tc.BOARD_EXT}"          # X.k9.kicad_pcb
RESUME_STALLED = f"{rp_cli.RESUME_FLAG} {rp_context.RESUME_STALLED}"
STALL_WORDS = ("`progress`", "`stalled`", "`stall_note`", RESUME_STALLED)
STALE_PROSE = ("copy the .kicad_pcb", "copy the `.kicad_pcb`", "copy the file first",
               "copy the kicad 9 file", "the copy you made", "ask the user to create it",
               "fix the designer-level tasks in the netlist and re-run")


def test_flow_prompt_frontmatter():
    problems: list[str] = []
    for path in FLOW_PROMPTS:
        meta = parse_frontmatter(_text(path))
        problems += [f"{_rel(path)}: no {k}" for k in REQUIRED_KEYS[path] if not meta.get(k)]
        want = path.parent.name if path.name == "SKILL.md" else path.stem
        if "name" in REQUIRED_KEYS[path] and meta.get("name") != want:
            problems.append(f"{_rel(path)}: name {meta.get('name')!r}, expected {want!r}")
    assert not problems, "\n".join(problems)


def test_flow_skill_descriptions_are_mutually_exclusive():
    """pcb-design, netlist-pipeline and napkin-mode: each description uses its own trigger
    wording, names the other two flows only in out-of-scope sentences ('not ... (use X)'),
    and uses another flow's trigger wording only inside those sentences."""
    problems: list[str] = []
    for name, path in FLOW_SKILLS.items():
        desc = parse_frontmatter(_text(path))["description"]
        others = [o for o in FLOW_SKILLS if o != name]
        sentences = _SENTENCE_END.split(_flat(desc))
        in_scope = [s.lower() for s in sentences
                    if not (_OUT_OF_SCOPE.search(s) and any(o in s for o in others))]
        problems += [f"{name} lacks its trigger {t!r}" for t in FLOW_TRIGGERS[name]
                     if t not in desc.lower()]
        for other in others:
            naming = [s for s in sentences if other in s]
            problems += [] if naming else [f"{name} never names {other} as out of scope"]
            problems += [f"{name} names {other} in scope: {s!r}" for s in naming
                         if not _OUT_OF_SCOPE.search(s)]
            problems += [f"{name} triggers on {other}'s wording {t!r}" for t in FLOW_TRIGGERS[other]
                         if any(t in s for s in in_scope)]
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("path", (PIPELINE_CMD, NETLIST_SKILL), ids=_rel)
def test_pipeline_prompts_name_flags_exit_codes_dims_and_artifacts(path):
    text = _flat(_text(path))
    wanted = [rp_cli.STRICT_FLAG, *(f"{f} {'|'.join(c)}" for f, c in rp_cli.CHOICES.items())]
    missing = [f for f in wanted if f not in text]
    missing += [f"`{code}` {word}" for code, word in EXIT_WORDS.items()
                if not re.search(rf"`{code}`[^`]{{0,24}}?\b{word}", text, re.I)]
    missing += [f"<stem>{s}" for s in ARTIFACT_SUFFIXES if f"<stem>{s}" not in text]
    runs = [s for s in _snippets(path) if "run_pipeline.py" in s]
    assert runs and not missing, f"{_rel(path)} lacks {missing or 'a run_pipeline.py line'}"
    required = [s for s in runs if re.search(r"(?<!\[)<w> <h>", s)]
    assert not required, f"board dims written as required (dims are optional): {required}"


def test_pipeline_command_runs_napkin_policies_and_the_routing_handoff():
    text = _flat(_text(PIPELINE_CMD))
    assert "${CLAUDE_PLUGIN_ROOT}/skills/napkin-mode/SKILL.md" in text
    assert {"router", "placement-reviewer-headless"} <= _subagents(PIPELINE_CMD)
    assert "napkin" in parse_frontmatter(_text(PIPELINE_CMD))["description"].lower()
    assert _keyword_values(_calls(PIPELINE_CMD), "record_decision", "stage"), \
        "the pipeline command never shows how a fix is recorded"


def test_new_board_stays_interactive_and_sends_hands_off_builds_to_napkin():
    text = _flat(_text(NEW_BOARD))
    assert "/kicad-eda:napkin" in text and "`pcb-design`" in text
    assert "parts-selector" in _subagents(NEW_BOARD)


@pytest.mark.parametrize("path", (ROUTER, ROUTING_SKILL), ids=_rel)
def test_routing_prompts_take_the_automatic_snapshot_and_stop_on_a_stall(path):
    text, calls = _flat(_text(path)), _calls(path)
    missing = [w for w in ("pre_k11_snapshot", SNAPSHOT_FILE, *STALL_WORDS) if w not in text]
    if not re.search(rf"\b{tc.STALL_ROUNDS}\b[^.]{{0,60}}\brounds?\b", text):
        missing.append(f"the {tc.STALL_ROUNDS}-round stall rule")
    assert not missing, f"{_rel(path)} lacks {missing}"
    assert "True" in _keyword_values(calls, "close_board", "save"), "no close_board(save=True)"
    assert "True" in _keyword_values(calls, "autoroute_board", "from_snapshot"), \
        "no autoroute_board(from_snapshot=True) fallback"


@pytest.mark.parametrize("path", (NETLIST_SKILL, PCB_SKILL), ids=_rel)
def test_flow_skills_describe_the_automatic_snapshot(path):
    text = _flat(_text(path))
    assert SNAPSHOT_FILE in text and "pre_k11_snapshot" in text, _rel(path)
    assert "True" in _keyword_values(_calls(path), "autoroute_board", "from_snapshot")


@pytest.mark.parametrize("path", FLOW_PROMPTS, ids=_rel)
def test_no_stale_manual_copy_or_designer_loop_prose(path):
    text = _flat(_text(path)).lower()
    stale = [p for p in STALE_PROSE if p in text]
    assert not stale, f"{_rel(path)} still says {stale}"


def test_netlist_skill_puts_every_stacked_pin_group_on_its_net():
    text = _flat(_text(NETLIST_SKILL))
    assert 'pin_groups["GND"]' in text and "stacked" in text and "no_connect" in text
