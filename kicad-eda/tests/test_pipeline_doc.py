"""docs/PIPELINE.md checked against the code (F5-20), so its claims are tested, never
self-attested: every `--flag` is accepted by the command line it belongs to (the harness's
rp_cli.parse_cli unless its line names another program), the exit-code table is rp_cli's, every
report key it names is a string literal of pipeline/run_pipeline.py or pipeline/rp_*.py, every
hook it names is in rp_hooks.HOOKS, the napkin / handoff / stall / review facts of sections 4a
and 4b match their modules, and every /kicad-eda: command, relative link and file-map path
exists. The file stays under 800 lines."""
from __future__ import annotations

import ast
import glob
import re
from functools import lru_cache
from pathlib import Path
from typing import Callable

import pytest

import decisions
import determinism_check
import napkin_policy
import netlist_schema
import review_report
import rp_cli
import rp_context
import rp_hooks
import rp_napkin
import rp_route_modes
import run_all
import safety_screen
import toolchain

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "PIPELINE.md"
PIPELINE_DIR = ROOT / "pipeline"
MAX_LINES = 800
EXIT_WORDS = {rp_cli.EXIT_PASS: "PASS", rp_cli.EXIT_FAIL: "FAIL", rp_cli.EXIT_USAGE: "usage",
              rp_cli.EXIT_HANDOFF: "handoff"}
# the report keys docs/PIPELINE.md must explain (the F5-20 list)
REQUIRED_REPORT_KEYS = ("route_cache_reason", "agent_board_preserved", "pipeline_report",
                        "pipeline_report_error", "error", "routing", "board_file",
                        "pre_k11_snapshot", "handoff", "decision_log_errors", "review_error")
REQUIRED_HOOKS = ("route_provider", "handoff_veto")
# programs whose options are not ours: each documented option is listed here on purpose
EXTERNAL_FLAGS: dict[str, frozenset[str]] = {"brew": frozenset({"--cask"}),
                                             "kicad-cli": frozenset({"--help"}),
                                             "winget": frozenset()}
HARNESS = "run_pipeline.py"
_PROGRAM = re.compile(r"(?<![\w.-])(run_pipeline\.py|determinism_check\.py|run_all\.py|"
                      r"netlist_schema\.py|kicad-cli|brew|winget)(?![\w-])")
# an option and the value(s) written after it: '--route auto', '--route=a|b', '--route a\|b'
_FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*)(?:[ =]([a-z]+(?:\\?\|[a-z]+)*))?")
_FENCE = re.compile(r"^```")
_SPAN = re.compile(r"`([^`\n]+)`")
_IDENT = re.compile(r"`([A-Za-z_]\w*)")
_COMMAND = re.compile(r"/kicad-eda:([a-z][\w-]*)")
_LINK = re.compile(r"\]\(([^)\s]+)\)")
_MAP_PATH = re.compile(r"(?<![\w/.-])((?:server|pipeline|skills|agents|commands|docs|tests|"
                       r"examples|scripts)/[\w./*<>-]*)")


# --- reading the doc ---------------------------------------------------------------------
@lru_cache(maxsize=None)
def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def _section(prefix: str) -> str:
    """The body of the '## <prefix>' section, up to the next '## ' heading."""
    m = re.search(rf"^## {re.escape(prefix)}[^\n]*$", _text(), re.M)
    assert m, f"docs/PIPELINE.md has no '## {prefix}...' section"
    nxt = re.search(r"^## ", _text()[m.end():], re.M)
    return _text()[m.end():m.end() + nxt.start()] if nxt else _text()[m.end():]


def _paragraph(section: str, lead: str) -> str:
    """The paragraph (or list block) of `section` that starts with `lead`."""
    m = re.search(rf"^{re.escape(lead)}.*?(?=\n\s*\n|\Z)", section, re.M | re.S)
    assert m, f"no paragraph starting with {lead!r}"
    return m.group(0)


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


# --- options -----------------------------------------------------------------------------
def _flag_uses() -> list[tuple[int, str, str, str]]:
    """(line number, program, option, values) of every '--option' in the doc. The program is
    the last one named before the option on its line, else (inside a code fence) the last one
    named in that fence, else the harness."""
    uses, fence, in_fence = [], None, False
    for number, line in enumerate(_text().splitlines(), 1):
        if _FENCE.match(line.strip()):
            in_fence, fence = not in_fence, None
            continue
        programs = [(m.start(), m.group(1)) for m in _PROGRAM.finditer(line)]
        for m in _FLAG.finditer(line):
            before = [name for pos, name in programs if pos < m.start()]
            program = before[-1] if before else (fence if in_fence and fence else HARNESS)
            uses.append((number, program, m.group(1), (m.group(2) or "").replace("\\", "")))
        if in_fence and programs:
            fence = programs[-1][1]
    return uses


def _harness_problem(flag: str, values: str) -> str | None:
    try:
        if flag not in rp_cli.CHOICES:
            rp_cli.parse_cli(["netlist.json", flag])
        for value in values.split("|") if values else []:
            rp_cli.parse_cli(["netlist.json", f"{flag}={value}"])
    except rp_cli.UsageError as e:
        return str(e)
    return None


def _determinism_problem(flag: str, _values: str) -> str | None:
    netlist = str(ROOT / "examples" / "lm358_buffer" / "netlist.json")
    errors = []
    for tail in ([flag], [flag, "2"]):
        try:
            determinism_check.parse_args([netlist, "--runs", "2", *tail])
            return None
        except determinism_check.UsageError as e:
            errors.append(str(e))
    return "; ".join(errors)


def _run_all_problem(flag: str, _values: str) -> str | None:
    return None if flag in (run_all.LIST_FLAG, "--help") else f"run_all.py has no {flag}"


def _schema_problem(flag: str, _values: str) -> str | None:
    return None if netlist_schema.main([flag]) == 0 else f"netlist_schema.py rejects {flag}"


CHECKERS: dict[str, Callable[[str, str], str | None]] = {
    HARNESS: _harness_problem, "determinism_check.py": _determinism_problem,
    "run_all.py": _run_all_problem, "netlist_schema.py": _schema_problem}


def test_every_flag_is_accepted_by_its_command_line(capsys: pytest.CaptureFixture) -> None:
    problems = []
    for number, program, flag, values in _flag_uses():
        if program in EXTERNAL_FLAGS:
            why = None if flag in EXTERNAL_FLAGS[program] else "not in EXTERNAL_FLAGS"
        else:
            why = CHECKERS[program](flag, values)
        if why:
            problems.append(f"line {number}: {program} {flag} {values}: {why}")
    capsys.readouterr()                                  # netlist_schema --schema prints it
    assert not problems, "\n".join(problems)


def test_the_harness_section_documents_every_option_and_value() -> None:
    harness = [(flag, values) for _n, program, flag, values in _flag_uses() if program == HARNESS]
    section = _section("4. ")
    named = {(flag, value) for flag, values in harness for value in values.split("|") if value}
    missing = [f"{flag} {value}" for flag, choices in rp_cli.CHOICES.items()
               for value in choices if (flag, value) not in named]
    missing += [rp_cli.STRICT_FLAG] if rp_cli.STRICT_FLAG not in section else []
    assert not missing, f"docs/PIPELINE.md never shows {missing}"


def test_exit_code_table_matches_rp_cli() -> None:
    codes = {v for k, v in vars(rp_cli).items() if k.startswith("EXIT_") and isinstance(v, int)}
    assert codes == set(EXIT_WORDS), "rp_cli has an exit code this test does not know"
    rows = {int(r[0].strip("`")): r[1] for r in _table(_section("4. "), "| Exit |")}
    assert set(rows) == codes, f"exit-code table {sorted(rows)} vs rp_cli {sorted(codes)}"
    wrong = [code for code, word in EXIT_WORDS.items()
             if not rows[code].lower().startswith(word.lower())]
    assert not wrong, f"exit-code rows {wrong} do not open with {[EXIT_WORDS[c] for c in wrong]}"
    stray = sorted({int(n) for n in re.findall(r"\bexit(?:s| code)?\s+`?(\d+)", _text(), re.I)}
                   - codes)
    assert not stray, f"docs/PIPELINE.md names exit code(s) {stray} rp_cli does not have"


# --- report keys ---------------------------------------------------------------------------
@lru_cache(maxsize=None)
def _harness_literals() -> frozenset[str]:
    files = [PIPELINE_DIR / "run_pipeline.py", *sorted(PIPELINE_DIR.glob("rp_*.py"))]
    return frozenset(node.value for f in files
                     for node in ast.walk(ast.parse(f.read_text(encoding="utf-8")))
                     if isinstance(node, ast.Constant) and isinstance(node.value, str))


def _report_keys() -> tuple[set[str], set[str]]:
    """(the keys of the report-key table, every key the doc names as a report key)."""
    table = {key for row in _table(_section("4. "), "| Report key")
             for key in _IDENT.findall(row[0])}
    named = set(re.findall(r"\breport(?:'s)?\s+`([A-Za-z_]\w*)", _text()))
    named |= set(re.findall(r"""report\[["'](\w+)["']\]""", _text()))
    return table, table | named


def test_report_keys_are_real_and_the_required_ones_are_documented() -> None:
    table, named = _report_keys()
    unknown = sorted(k for k in named if k not in _harness_literals())
    assert not unknown, f"report keys not in pipeline/run_pipeline.py or rp_*.py: {unknown}"
    missing = [k for k in REQUIRED_REPORT_KEYS if k not in table]
    assert not missing, f"the report-key table lacks {missing}"


# --- hooks --------------------------------------------------------------------------------
def test_every_hook_named_is_in_the_rp_hooks_table() -> None:
    table = {name for row in _table(_section("4. "), "| Hooks") for cell in row[1:]
             for name in _IDENT.findall(cell)}
    prose = set(re.findall(r"`(\w+)`\s+hook", _text())) | set(
        re.findall(r"\bhooks?\s+`(\w+)`", _text()))
    unknown = sorted((table | prose) - set(rp_hooks.HOOKS))
    assert not unknown, f"hooks not in rp_hooks.HOOKS: {unknown}"
    assert set(REQUIRED_HOOKS) <= table, f"the hook table lacks {set(REQUIRED_HOOKS) - table}"
    assert rp_hooks.ENV_VAR in _text() and "rp_feature_<name>.py" in _text()


# --- 4a: snapshot, stall rule, handoff, resume -------------------------------------------
def _dict_keys(func: Callable[..., object]) -> set[str]:
    """The string keys of the dict literals in `func`'s source."""
    tree = ast.parse(Path(func.__code__.co_filename).read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == func.__name__)
    return {k.value for d in ast.walk(node) if isinstance(d, ast.Dict) for k in d.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def test_agent_routing_section_states_the_snapshot_stall_and_handoff_rules() -> None:
    sec = _section("4a.")
    for fact in (f"X{toolchain.SNAPSHOT_TAG}.kicad_pcb", f"X{toolchain.AGENT_TAG}.<n>.*",
                 f"{toolchain.STALL_ROUNDS} rounds", "progress_note", "release_for_rewrite",
                 f"<stem>{rp_route_modes.HANDOFF_SUFFIX}", rp_route_modes.HANDOFF_KIND,
                 *(f"--resume {mode}" for mode in rp_context.RESUME_MODES)):
        assert fact in sec, f"section 4a does not state {fact!r}"
    handoff = _paragraph(sec, "**Handoff**")
    named = set(_IDENT.findall(handoff))
    keys = _dict_keys(rp_route_modes.handoff_document) - {"unconnected_items_error",
                                                          "net_classes_error"}
    assert keys <= named, f"handoff fields not documented: {sorted(keys - named)}"


# --- 4b: napkin mode -----------------------------------------------------------------------
def test_napkin_section_matches_the_decision_log_review_and_policies() -> None:
    sec = _section("4b.")
    facts = [f"<stem>{decisions.DECISIONS_SUFFIX}", f"<log>{decisions.LOCK_SUFFIX}",
             *decisions.ENTRY_FIELDS, *decisions.STAGES[:2], "G1..G5", decisions.STAGES[-1],
             *decisions.CONFIDENCE_RANK, f"<stem>{review_report.REVIEW_SUFFIX}",
             f"<stem>{rp_napkin.NAPKIN_TAG}.kicad_sch", safety_screen.HUMAN_REVIEW,
             "safety_screen.detect_safety", *napkin_policy.NAPKIN_FIX_KINDS,
             f"{rp_napkin.G3_POLICY_ROUNDS} policy rounds", "--strict", '"mode": "napkin"',
             "record_decision", "pure ASCII", "fallback"]
    missing = [f for f in facts if f not in sec]
    assert not missing, f"section 4b does not state {missing}"
    assert safety_screen.detect_safety.__doc__, "the safety heuristics' source of truth is gone"
    review = _paragraph(sec, "**Review.**")
    listed = {s for s in re.findall(r"\*\*([^*]+)\*\*", review) if s != "Review."}
    unknown = listed - set(review_report.SECTIONS)
    assert listed and not unknown, f"review sections not in review_report.SECTIONS: {unknown}"


# --- commands, links, file map, size ---------------------------------------------------------
def test_commands_and_relative_links_resolve() -> None:
    problems = [f"/kicad-eda:{c}" for c in _COMMAND.findall(_text())
                if not (ROOT / "commands" / f"{c}.md").is_file()
                and not (ROOT / "skills" / c / "SKILL.md").is_file()]
    problems += [t for t in _LINK.findall(_text()) if not re.match(r"^(https?:|mailto:|#)", t)
                 and not (DOC.parent / t.split("#")[0]).exists()]
    assert not problems, problems


def test_file_map_paths_exist() -> None:
    block = re.search(r"```\n(.*?)```", _section("9."), re.S)
    assert block, "section 9 has no file-map block"
    paths = [p.rstrip(".,;:)") for p in _MAP_PATH.findall(block.group(1))]
    missing = [p for p in paths if not glob.glob(str(ROOT / re.sub(r"<[^>]*>", "*", p)))]
    assert paths and not missing, f"file-map paths that do not exist: {missing}"


def test_doc_stays_under_the_line_limit() -> None:
    lines = _text().count("\n")
    assert lines < MAX_LINES, f"docs/PIPELINE.md has {lines} lines (limit {MAX_LINES})"
