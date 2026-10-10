"""Doc claims of the project-library guide (F2-18), checked against the code.

docs/LIBRARIES.md, skills/netlist-pipeline/libraries.md and
agents/parts-selector.md name MCP tools, Python helpers, lint codes, file
names and KiCad dialect limits. Each claim is checked here against the code
that implements it, never by self-attestation:

  * every call written in code (`name(...)` in a code span or a python
    fence) names a served MCP tool (the TOOLS of kicad_server.TOOL_MODULES)
    or, dotted, a function of a plugin module, with only parameters its
    signature has (a bare-name positional argument must be the parameter at
    that position); a tool-shaped name (author_*, validate_*, resolve_*,
    compare_*, record_*, import_*) is a served tool;
  * the JSON examples in those calls build (fp_spec_json, sym_author) and
    their literal values are valid (presets, templates, decision stages);
  * every LIB-* code named exists in lint_rules_libs, and LIBRARIES.md
    documents every code the module emits;
  * constants (managed-row descr, dialect versions, default paths, sidecar
    names, route-hash key, fallback stage, review sources, lint usage) match
    the code;
  * relative links, ${CLAUDE_PLUGIN_ROOT} paths and /kicad-eda: commands exist;
  * the retired "custom symbols are not supported" guidance is gone from the
    prompts, and docs/SETUP.md carries the api -> file fallback and the
    KiCad 9/10 footprint dialect limits.
"""
from __future__ import annotations

import ast
import builtins
import importlib
import inspect
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import pytest

import decisions
import fp_dialect
import fp_spec_json
import kicad_dialect
import kicad_server
import libtable
import library_tools
import lint_netlist
import netlist_schema
import part_presets
import project_libs
import review_sources
import review_tools
import rp_feature_libs
import sch_stage_tools
import sym_author

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "LIBRARIES.md"
SKILL_REF = ROOT / "skills" / "netlist-pipeline" / "libraries.md"
AGENT = ROOT / "agents" / "parts-selector.md"
SETUP = ROOT / "docs" / "SETUP.md"
DOCS = (GUIDE, SKILL_REF, AGENT)
MODULE_DIRS = (ROOT / "server", ROOT / "server" / "tools", ROOT / "pipeline")
PROMPT_GLOBS = ("agents/*.md", "commands/*.md", "skills/**/*.md", "docs/*.md", "README.md")
RETIRED = ("custom symbols are not supported", "ask the user to create it",
           "must create it in the symbol editor")
AGENT_KEYS = ("name", "description", "model", "effort", "maxTurns")
CALL_LANGS = ("", "python", "py")          # fences whose calls are checked
TOOL_SHAPED = re.compile(r"^(?:author|validate|resolve|compare|record|import)_[a-z0-9_]+$")
LIB_CODE = re.compile(r"\bLIB-[EW]\d+\b")
_FENCE = re.compile(r"^```([^\n`]*)\n(.*?)^```", re.M | re.S)
_SPAN = re.compile(r"`([^`\n]+)`")
_CALL = re.compile(r"(?<![\w.$/<-])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\(")
_KEYWORD = re.compile(r"^([A-Za-z_]\w*)\s*=(?!=)\s*(.*)$", re.S)
_IDENT = re.compile(r"^[A-Za-z_]\w*$")
_LINK = re.compile(r"\]\(([^)\s]+)\)")
_PLUGIN_PATH = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./-]+)")
_COMMAND = re.compile(r"/kicad-eda:([a-z][\w-]*)")
_CONSTANTS = {"True", "False", "None"}
_OPEN, _CLOSE, _QUOTES = "([{", ")]}", "\"'"


# --- reading the docs ------------------------------------------------------------------
@lru_cache(maxsize=None)
def _text(path: Path) -> str:
    assert path.is_file(), f"{path.relative_to(ROOT)} is missing"
    return path.read_text(encoding="utf-8")


def _snippets(path: Path) -> list[tuple[str, str]]:
    """(language, code) of every fence and every inline code span outside them."""
    text = _text(path)
    fences = [(m.group(1).strip().lower(), m.group(2)) for m in _FENCE.finditer(text)]
    prose = _FENCE.sub("", text)
    return fences + [("", m.group(1)) for m in _SPAN.finditer(prose)]


def _closing(text: str, start: int) -> int:
    """Index of the ')' closing the '(' before ``start`` (-1 when unbalanced)."""
    depth, quote = 0, ""
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            quote = "" if ch == quote and text[i - 1] != "\\" else quote
        elif ch in _QUOTES:
            quote = ch
        elif ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            if depth == 0:
                return i if ch == ")" else -1
            depth -= 1
    return -1


def _split_args(text: str) -> tuple[str, ...]:
    """Top-level comma-separated arguments (brackets and quotes respected)."""
    args, depth, quote, start = [], 0, "", 0
    for i, ch in enumerate(text):
        if quote:
            quote = "" if ch == quote and text[i - 1] != "\\" else quote
        elif ch in _QUOTES:
            quote = ch
        elif ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(text[start:i].strip())
            start = i + 1
    tail = text[start:].strip()
    return tuple(a for a in [*args, tail] if a)


@dataclass(frozen=True)
class Call:
    doc: str
    lang: str
    name: str
    args: tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.doc}: {self.name}({', '.join(self.args)})"

    def keywords(self) -> dict[str, str]:
        return {m.group(1): m.group(2).strip() for a in self.args if (m := _KEYWORD.match(a))}


def _calls(path: Path) -> list[Call]:
    found = []
    for lang, code in _snippets(path):
        if lang not in CALL_LANGS:
            continue
        for m in _CALL.finditer(code):
            end = _closing(code, m.end())
            args = _split_args(code[m.end():end]) if end >= 0 else ("<unbalanced>",)
            found.append(Call(path.name, lang, m.group(1), args))
    return found


def _literal(value: str) -> Any:
    """A keyword value written in a doc: JSON / Python literal, else the raw text."""
    for parse in (json.loads, ast.literal_eval):
        try:
            return parse(value)
        except (ValueError, SyntaxError):
            continue
    return value


# --- what the code serves ----------------------------------------------------------------
@lru_cache(maxsize=None)
def _served() -> dict[str, Callable[..., Any]]:
    return {fn.__name__: fn for mod in kicad_server.TOOL_MODULES for fn in mod.TOOLS}


def _plugin_module(name: str) -> bool:
    return any((d / f"{name}.py").is_file() for d in MODULE_DIRS)


def _target(call: Call) -> Callable[..., Any] | None | str:
    """The function a call names, None to skip it, or why it names nothing."""
    head, *rest = call.name.split(".")
    if not rest:
        if head in _served():
            return _served()[head]
        if call.lang and hasattr(builtins, head):
            return None
        return f"{call}: {head} is not a served MCP tool"
    if not _plugin_module(head):
        return None if call.lang else f"{call}: {head} is not a plugin module"
    obj: Any = importlib.import_module(head)
    for part in rest:
        if not hasattr(obj, part):
            return f"{call}: {head} has no {'.'.join(rest)}"
        obj = getattr(obj, part)
    return obj if callable(obj) else f"{call}: {call.name} is not callable"


@dataclass(frozen=True)
class _Params:
    """What a signature accepts: positional names in order, every name, *args / **kw."""
    positional: tuple[str, ...]
    named: frozenset[str]
    var_pos: bool
    var_kw: bool

    @classmethod
    def of(cls, fn: Callable[..., Any]) -> _Params:
        params = inspect.signature(fn).parameters.values()
        kinds = inspect.Parameter
        return cls(tuple(p.name for p in params
                         if p.kind in (kinds.POSITIONAL_ONLY, kinds.POSITIONAL_OR_KEYWORD)),
                   frozenset(p.name for p in params if p.kind is not kinds.VAR_POSITIONAL),
                   any(p.kind is kinds.VAR_POSITIONAL for p in params),
                   any(p.kind is kinds.VAR_KEYWORD for p in params))


def _positional_problem(call: Call, arg: str, index: int, exact: bool, sig: _Params) -> str:
    """Why positional ``arg`` (#index) does not fit ('' = it does)."""
    if sig.var_pos:
        return ""
    if index >= len(sig.positional):
        return f"{call}: too many positional arguments ({list(sig.positional)})"
    if not _IDENT.match(arg) or arg in _CONSTANTS:
        return ""                                  # a literal value: any parameter takes it
    fits = arg == sig.positional[index] if exact else arg in sig.positional
    return "" if fits else (f"{call}: positional {arg!r} is not parameter #{index + 1} "
                            f"({list(sig.positional)})")


def _signature_problems(call: Call, fn: Callable[..., Any]) -> list[str]:
    """``call``'s arguments against ``fn``'s signature ('...' = arguments left out)."""
    sig = _Params.of(fn)
    problems, index, exact, keyword_seen = [], 0, True, False
    for arg in call.args:
        if arg == "...":
            exact = False
        elif m := _KEYWORD.match(arg):
            keyword_seen = True
            if m.group(1) not in sig.named and not sig.var_kw:
                problems.append(f"{call}: no parameter {m.group(1)!r} (has {sorted(sig.named)})")
        elif keyword_seen:
            problems.append(f"{call}: positional {arg!r} after a keyword argument")
        else:
            problems.append(_positional_problem(call, arg, index, exact, sig))
            index += 1
    return [p for p in problems if p]


def _all_calls() -> list[Call]:
    return [c for doc in DOCS for c in _calls(doc)]


def _calls_of(name: str, docs: tuple[Path, ...] = DOCS) -> list[Call]:
    return [c for doc in docs for c in _calls(doc) if c.name == name]


def _calls_of_any(docs: tuple[Path, ...]) -> list[Call]:
    return [c for doc in docs for c in _calls(doc)]


# --- calls and tool names ------------------------------------------------------------------
def test_every_call_names_a_served_tool_or_plugin_function_with_real_parameters():
    problems = []
    for call in _all_calls():
        target = _target(call)
        if isinstance(target, str):
            problems.append(target)
        elif target is not None:
            problems += _signature_problems(call, target)
    assert not problems, "\n".join(problems)


def test_tool_shaped_names_are_served_tools():
    bad = [f"{doc.name}: `{code}`" for doc in DOCS for lang, code in _snippets(doc)
           if not lang and TOOL_SHAPED.match(code.strip()) and code.strip() not in _served()]
    assert not bad, "not served MCP tools (deferred or misspelt):\n" + "\n".join(bad)


def test_standalone_parameter_mentions_belong_to_a_tool_the_doc_calls():
    problems = []
    for doc in DOCS:
        params = {p for c in _calls(doc) if not isinstance(t := _target(c), str) and t
                  for p in inspect.signature(t).parameters}
        for lang, code in _snippets(doc):
            m = _KEYWORD.match(code.strip())
            if not lang and m and not _CALL.search(code) and m.group(1) not in params:
                problems.append(f"{doc.name}: `{code}` names no parameter of a tool it calls")
    assert not problems, "\n".join(problems)


def test_the_library_tools_are_all_documented():
    called = {c.name for c in _calls_of_any((GUIDE, SKILL_REF))}
    missing = [fn.__name__ for fn in library_tools.TOOLS if fn.__name__ not in called]
    assert not missing, f"library tools never called in the guides: {missing}"
    agent = {c.name for c in _calls(AGENT)}
    want = {"author_footprint", "author_symbol", "validate_library", "resolve_libraries",
            review_tools.record_decision.__name__}
    assert want <= agent, f"parts-selector.md does not call {sorted(want - agent)}"


def test_decision_calls_use_valid_stage_and_confidence():
    calls = _calls_of("record_decision")
    assert any(_literal(c.keywords().get("stage", "")) == "parts" for c in calls)
    for call in calls:
        kw = {k: _literal(v) for k, v in call.keywords().items()}
        assert kw.get("stage", "parts") in decisions.STAGES, str(call)
        assert kw.get("confidence", "high") in netlist_schema.CONFIDENCE_LEVELS, str(call)


# --- literal values in the calls -----------------------------------------------------------
def test_presets_and_templates_named_exist():
    for call in _calls_of("author_footprint") + _calls_of("author_symbol"):
        kw = {k: _literal(v) for k, v in call.keywords().items()}
        if "preset" in kw:
            part_presets.get_preset(kw["preset"], opt_in=bool(kw.get("opt_in")))
        if "template" in kw:
            assert kw["template"] in fp_spec_json.TEMPLATES, str(call)


def test_documented_footprint_and_symbol_examples_build():
    built = 0
    for call in _calls_of("author_footprint"):
        kw = {k: _literal(v) for k, v in call.keywords().items()}
        if isinstance(kw.get("params"), dict) and "template" in kw:
            params = {**kw["params"], **({"name": kw["name"]} if "name" in kw else {})}
            fp_spec_json.spec_from_json(kw["template"], params)
            built += 1
    for call in _calls_of("author_symbol"):
        spec = _literal(call.keywords().get("spec", ""))
        if isinstance(spec, dict):
            sym_author.layout(sym_author.spec_from_dict(spec))
            built += 1
    assert built >= 2, "the skill reference needs a template example and a symbol spec example"


def test_every_preset_and_template_is_listed():
    text = _text(SKILL_REF)
    missing = [p for p in [*part_presets.PRESETS, *fp_spec_json.TEMPLATES] if f"`{p}`" not in text
               and f'"{p}"' not in text]
    assert not missing, f"libraries.md does not list {missing}"
    opt_in = [c for c in _calls_of("author_footprint", (SKILL_REF,))
              if _literal(c.keywords().get("preset", "")) in part_presets.OPT_IN_PRESETS]
    assert opt_in and all(c.keywords().get("opt_in") == "True" for c in opt_in)


# --- lint codes ---------------------------------------------------------------------------
@lru_cache(maxsize=None)
def _emitted_codes() -> frozenset[str]:
    """LIB-* codes in lint_rules_libs' string literals, its docstrings excluded."""
    tree = ast.parse((ROOT / "server" / "tools" / "lint_rules_libs.py").read_text("utf-8"))
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
            and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    return frozenset(code for node in ast.walk(tree)
                     if isinstance(node, ast.Constant) and isinstance(node.value, str)
                     and id(node) not in docs for code in LIB_CODE.findall(node.value))


def test_lib_codes_exist_and_the_guide_lists_them_all():
    emitted = _emitted_codes()
    assert {"LIB-E1", "LIB-W1"} <= emitted
    for doc in DOCS:
        unknown = set(LIB_CODE.findall(_text(doc))) - emitted
        assert not unknown, f"{doc.name} names codes lint_rules_libs never emits: {unknown}"
    undocumented = emitted - set(LIB_CODE.findall(_text(GUIDE)))
    assert not undocumented, f"LIBRARIES.md does not document {sorted(undocumented)}"


def test_lint_usage_matches_the_cli():
    usage = lint_netlist.USAGE.removeprefix("Usage: ")
    assert usage in _text(GUIDE)


# --- facts --------------------------------------------------------------------------------
def test_managed_rows_and_resolution_order():
    text = _text(GUIDE)
    assert f'(descr "{libtable.MANAGED_DESCR}")' in text
    assert libtable.SOURCES == ("netlist", "project", "stock")
    assert "block > project tables > stock" in text
    for name in libtable.TABLE_FILES.values():
        assert f"`{name}`" in text


def test_dialect_limits_match_fp_dialect():
    k9, k10 = fp_dialect.KICAD9_FOOTPRINT_VERSION, fp_dialect.KICAD10_FOOTPRINT_VERSION
    assert fp_dialect.FOOTPRINT_LIMITS == {9: k9, 10: k10}
    for doc in (GUIDE, SETUP):
        assert str(k9) in _text(doc) and str(k10) in _text(doc), doc.name
    assert str(kicad_dialect.K9_SYM_VERSION) in _text(GUIDE)
    assert "kicad_dialect.py fp" in _text(GUIDE)


def test_default_library_paths_and_sidecars():
    nick = "<name>" + project_libs.PARTS_SUFFIX
    for kind in libtable.KINDS:
        path = project_libs.default_library_path(kind, nick)
        assert path in _text(GUIDE) and path in _text(SKILL_REF), path
    sidecar = f"{project_libs.PINS_SUBDIR}/<nick>_<symbol>.pins.json"
    assert sidecar in _text(GUIDE)
    assert 'f"{nick}_{symbol}.pins.json"' in inspect.getsource(sym_author.write_pin_sidecar)


def test_route_hash_digest_key(tmp_path):
    extra = rp_feature_libs.route_hash_extra({"components": []}, tmp_path)
    (key,) = extra
    text = _text(GUIDE)
    assert f"`{key}`" in text and "ROUTE_HASH_EXCLUDED" in text
    assert rp_feature_libs.ROUTE_HASH_EXCLUDED == ("libraries",)


def test_api_fallback_is_described():
    stage = sch_stage_tools.FALLBACK_STAGE
    guide, setup = _text(GUIDE), _text(SETUP)
    assert f"`{stage}`" in guide and "`fallback`" in guide and "`fallback`" in setup
    assert decisions.DECISIONS_SUFFIX in guide and decisions.DECISIONS_SUFFIX in setup
    assert "Until then it has no library tables" not in setup


def test_review_sources_are_named():
    shown = " | ".join(s for s in review_sources.SOURCES if s != review_sources.UNKNOWN)
    assert shown == "stock | project | generated"
    for doc in (GUIDE, AGENT):
        assert shown in _text(doc), doc.name


# --- links, commands, retired prose --------------------------------------------------------
def test_links_plugin_paths_and_commands_resolve():
    problems = []
    for doc in DOCS:
        text = _text(doc)
        for target in _LINK.findall(text):
            if not re.match(r"^(https?:|mailto:|#)", target):
                path = (doc.parent / target.split("#")[0]).resolve()
                problems += [] if path.exists() else [f"{doc.name}: link {target}"]
        problems += [f"{doc.name}: {p}" for p in _PLUGIN_PATH.findall(text)
                     if not (ROOT / p.rstrip(".")).exists()]
        problems += [f"{doc.name}: /kicad-eda:{c}" for c in _COMMAND.findall(text)
                     if not (ROOT / "commands" / f"{c}.md").is_file()
                     and not (ROOT / "skills" / c / "SKILL.md").is_file()]
    assert not problems, "\n".join(problems)


def test_retired_custom_part_guidance_is_gone():
    files = sorted({p for g in PROMPT_GLOBS for p in ROOT.glob(g) if p.is_file()})
    hits = [f"{p.relative_to(ROOT)}: {phrase!r}" for p in files for phrase in RETIRED
            if phrase in p.read_text(encoding="utf-8").lower()]
    assert not hits, "\n".join(hits)


def _frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n"), "no frontmatter"
    head = text.split("\n---\n", 1)[0].splitlines()[1:]
    return {k.strip(): v.strip() for k, _, v in (line.partition(":") for line in head) if v}


@pytest.mark.parametrize("phrase", ["napkin", "pin_groups", "netlist_hints",
                                    "Symbol Editor", "libs/<name>_parts"])
def test_parts_selector_covers_both_flows(phrase):
    assert phrase in _text(AGENT)


def test_parts_selector_frontmatter():
    meta = _frontmatter(_text(AGENT))
    assert all(meta.get(k) for k in AGENT_KEYS), meta
    assert meta["name"] == AGENT.stem
