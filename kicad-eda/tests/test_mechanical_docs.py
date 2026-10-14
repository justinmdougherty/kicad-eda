"""Doc claims of the mechanical / outline guide (F3-22), checked against the code.

docs/MECHANICAL.md (the reference) and skills/netlist-pipeline/outline.md (the
agent-facing summary) name scripts, MCP tools, plugin functions, lint and gate
codes, kicad-cli flags, file suffixes and tolerances. Each claim is checked here
against the code that implements it, never by self-attestation:

  * every ``python <script>`` / ``<script>.py`` command in a fenced block names
    an existing script; a venv script's ``--help`` exits 0 (run with the venv's
    Python, stdin closed); a KiCad-python script (one that imports pcbnew) is
    checked for existence only;
  * every tool / function name in backticks exists: a call ``name(...)`` names
    a served MCP tool (the TOOLS of kicad_server.TOOL_MODULES) or, dotted, a
    function of a plugin module, with only parameters its signature has; a
    dotted ``module.attr`` whose head is a plugin module names an attribute of
    it; a tool-shaped bare name is a served tool (or a plugin module);
  * the six mechanical tools are all called in the reference and named in the
    summary; every EO* / WO* code lint_rules_outline emits is in the reference's
    G1 table and no doc names one it does not emit; every G3 / G4 code of
    swig_outline_checks is in the reference;
  * the numbers quoted (join tolerance, Hausdorff tolerance, chord error, DSN
    margin, connector rule, straddle tolerance, field margins, fab limits,
    sheet margin) and the names (sidecar suffix, env var, mech folder, DXF
    layer / version / units, kicad-cli flags) match their modules;
  * relative links, ${CLAUDE_PLUGIN_ROOT} paths and /kicad-eda: commands exist.
"""
from __future__ import annotations

import ast
import inspect
import re
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import pytest

import board_field
import build_features
import dsn_outline
import dxf_io
import e2e_outline_check
import edge_mount
import kicad_server
import lint_rules_outline
import mech_export
import mechanical_tools
import outline
import outline_audit
import outline_model
import outline_ops
import outline_resolve
import review_mechanical
import rp_feature_outline
import swig_outline_checks

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "MECHANICAL.md"
SKILL_REF = ROOT / "skills" / "netlist-pipeline" / "outline.md"
DOCS = (GUIDE, SKILL_REF)
MODULE_DIRS = (ROOT / "server", ROOT / "server" / "tools", ROOT / "server" / "backends",
               ROOT / "pipeline")
HELP_TIMEOUT_S = 120.0
MATH_NAMES = frozenset({"abs", "max", "min", "sqrt", "tan", "cos", "atan"})   # not tools
TOOL_SHAPED = re.compile(r"^(?:import|write|export|validate|build|autoroute|check|record|"
                         r"review|resolve|route|open|save|render|design)_[a-z0-9_]+$")
# the reference's sections, in order (the F3-22 task body)
SECTIONS = ("Frames", "Board block", "DXF in", "DXF out", "STEP", "Placement", "Gates",
            "Fab rules", "Freerouting", "KiCad 9", "Troubleshooting")
_FENCE = re.compile(r"^```([^\n`]*)\n(.*?)^```", re.M | re.S)
_SPAN = re.compile(r"`([^`\n]+)`")
_SCRIPT = re.compile(r"(?:server|pipeline|tests|scripts)[\\/][\w\\/.-]*?\.py\b")
_CALL_SPAN = re.compile(r"^([A-Za-z_][\w.]*)\((.*)\)$", re.S)
_DOTTED_SPAN = re.compile(r"^([a-z_][a-z0-9_]*)\.([A-Za-z_][\w.]*)$")
_KEYWORD = re.compile(r"^([A-Za-z_]\w*)\s*=(?!=)\s*(.*)$", re.S)
_IDENT = re.compile(r"^[A-Za-z_]\w*$")
_LINT_CODE = re.compile(r"\b[EW]O\d\b")
_LINK = re.compile(r"\]\(([^)\s]+)\)")
_PLUGIN_PATH = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./-]+)")
_COMMAND = re.compile(r"/kicad-eda:([a-z][\w-]*)")
_IMPORTS_PCBNEW = re.compile(r"^\s*import pcbnew\b", re.M)
_CONSTANTS = {"True", "False", "None"}
_OPEN, _CLOSE, _QUOTES = "([{", ")]}", "\"'"


# --- reading the docs ------------------------------------------------------------------
@lru_cache(maxsize=None)
def _text(path: Path) -> str:
    assert path.is_file(), f"{path.relative_to(ROOT)} is missing"
    return path.read_text(encoding="utf-8")


def _fences(path: Path) -> list[tuple[str, str]]:
    return [(m.group(1).strip().lower(), m.group(2)) for m in _FENCE.finditer(_text(path))]


def _spans(path: Path) -> list[str]:
    """Every inline code span outside the fences."""
    return [m.group(1) for m in _SPAN.finditer(_FENCE.sub("", _text(path)))]


def _split_args(text: str) -> tuple[str, ...]:
    """Top-level comma-separated arguments (brackets and quotes respected)."""
    args: list[str] = []
    depth, quote, start = 0, "", 0
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
    name: str
    args: tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.doc}: {self.name}({', '.join(self.args)})"


def _calls(path: Path) -> list[Call]:
    found = []
    for span in _spans(path):
        m = _CALL_SPAN.match(span.strip())
        if m:
            found.append(Call(path.name, m.group(1), _split_args(m.group(2))))
    return found


# --- what the code has ---------------------------------------------------------------------
@lru_cache(maxsize=None)
def _served() -> dict[str, Callable[..., Any]]:
    return {fn.__name__: fn for mod in kicad_server.TOOL_MODULES for fn in mod.TOOLS}


def _plugin_module(name: str) -> bool:
    return any((d / f"{name}.py").is_file() for d in MODULE_DIRS)


def _attribute(name: str) -> Any | str:
    """The object a dotted plugin name points at, or why there is none."""
    head, *rest = name.split(".")
    obj: Any = sys.modules.get(head)
    if obj is None:
        try:
            obj = __import__(head)
        except ImportError as e:            # a KiCad-python module: pcbnew is missing here
            return f"{head} is not importable in the venv ({e})"
    for part in rest:
        if not hasattr(obj, part):
            return f"{head} has no attribute {'.'.join(rest)}"
        obj = getattr(obj, part)
    return obj


def _target(call: Call) -> Callable[..., Any] | None | str:
    """The function a call names, None to skip it (a pcbnew / KiCad API call such
    as ``zone.Outline()`` or ``BuildCourtyardCaches()``, whose head is CamelCase
    or no plugin module), or why it names nothing."""
    head, *rest = call.name.split(".")
    if not rest:
        if head in _served():
            return _served()[head]
        if head[0].isupper() or head in MATH_NAMES:
            return None
        return f"{call}: {head} is not a served MCP tool"
    if not _plugin_module(head):
        return None
    obj = _attribute(call.name)
    if isinstance(obj, str):
        return f"{call}: {obj}"
    return obj if callable(obj) else f"{call}: {call.name} is not callable"


def _signature_problems(call: Call, fn: Callable[..., Any]) -> list[str]:
    """``call``'s arguments against ``fn``'s signature ('...' = arguments left out)."""
    params = list(inspect.signature(fn).parameters.values())
    positional = [p.name for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    named = {p.name for p in params if p.kind is not p.VAR_POSITIONAL}
    var_kw = any(p.kind is p.VAR_KEYWORD for p in params)
    problems: list[str] = []
    index, exact = 0, True
    for arg in call.args:
        if arg == "...":
            exact = False
            continue
        if m := _KEYWORD.match(arg):
            if m.group(1) not in named and not var_kw:
                problems.append(f"{call}: no parameter {m.group(1)!r} (has {sorted(named)})")
            continue
        if index >= len(positional):
            problems.append(f"{call}: too many positional arguments ({positional})")
        elif _IDENT.match(arg) and arg not in _CONSTANTS and not (
                arg == positional[index] if exact else arg in positional):
            problems.append(f"{call}: positional {arg!r} is not parameter #{index + 1} "
                            f"({positional})")
        index += 1
    return problems


# --- fenced commands -------------------------------------------------------------------------
def _scripts() -> list[Path]:
    found: dict[str, Path] = {}
    for doc in DOCS:
        if not doc.is_file():               # a missing doc fails the tests, not collection
            continue
        for _lang, code in _fences(doc):
            for m in _SCRIPT.finditer(code):
                rel = m.group(0).replace("\\", "/")
                found.setdefault(rel, ROOT / rel)
    return [found[k] for k in sorted(found)]


def _is_kicad_python(script: Path) -> bool:
    return bool(_IMPORTS_PCBNEW.search(script.read_text(encoding="utf-8", errors="replace")))


def test_fenced_commands_name_scripts():
    assert _scripts(), "the guides show no script command in a fenced block"


@pytest.mark.parametrize("script", _scripts(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_fenced_script_exists_and_help_exits_0(script: Path):
    assert script.is_file(), f"{script.relative_to(ROOT)} does not exist"
    if _is_kicad_python(script):
        return                              # runs under KiCad's Python: existence only
    proc = subprocess.run([sys.executable, str(script), "--help"], cwd=ROOT,
                          stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
                          errors="replace", timeout=HELP_TIMEOUT_S, check=False)
    assert proc.returncode == 0, (f"{script.relative_to(ROOT)} --help exited "
                                  f"{proc.returncode}: {(proc.stderr or proc.stdout)[-600:]}")


# --- names in backticks ----------------------------------------------------------------------
def test_every_call_names_a_served_tool_or_plugin_function_with_real_parameters():
    problems = []
    for doc in DOCS:
        for call in _calls(doc):
            target = _target(call)
            if isinstance(target, str):
                problems.append(target)
            elif target is not None:
                problems += _signature_problems(call, target)
    assert not problems, "\n".join(problems)


def test_dotted_plugin_names_are_attributes():
    problems = []
    for doc in DOCS:
        for span in _spans(doc):
            m = _DOTTED_SPAN.match(span.strip())
            if m and _plugin_module(m.group(1)):
                found = _attribute(span.strip())
                if isinstance(found, str):
                    problems.append(f"{doc.name}: `{span}`: {found}")
    assert not problems, "\n".join(problems)


def test_tool_shaped_bare_names_are_served_tools():
    bad = [f"{doc.name}: `{span}`" for doc in DOCS for span in _spans(doc)
           if TOOL_SHAPED.match(span.strip()) and span.strip() not in _served()
           and not _plugin_module(span.strip())]
    assert not bad, "not served MCP tools (deferred or misspelt):\n" + "\n".join(bad)


def test_the_mechanical_tools_are_all_documented():
    names = [fn.__name__ for fn in mechanical_tools.TOOLS]
    assert len(names) == 6
    called = {c.name for c in _calls(GUIDE)}
    missing = [n for n in names if n not in called]
    assert not missing, f"MECHANICAL.md never calls {missing}"
    spans = {s.strip().split("(")[0] for s in _spans(SKILL_REF)}
    assert not [n for n in names if n not in spans], "outline.md does not name every tool"


# --- lint and gate codes ---------------------------------------------------------------------
@lru_cache(maxsize=None)
def _emitted_lint_codes() -> frozenset[str]:
    """EO* / WO* codes in lint_rules_outline's string literals, docstrings excluded."""
    tree = ast.parse((ROOT / "server" / "tools" / "lint_rules_outline.py").read_text("utf-8"))
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
            and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    return frozenset(code for node in ast.walk(tree)
                     if isinstance(node, ast.Constant) and isinstance(node.value, str)
                     and id(node) not in docs for code in _LINT_CODE.findall(node.value))


def test_lint_codes_exist_and_the_guide_lists_them_all():
    emitted = _emitted_lint_codes()
    assert {"EO1", "EO2", "WO1"} <= emitted
    assert lint_rules_outline.ORDER == 30
    for doc in DOCS:
        unknown = set(_LINT_CODE.findall(_text(doc))) - emitted
        assert not unknown, f"{doc.name} names codes lint_rules_outline never emits: {unknown}"
    undocumented = emitted - set(_LINT_CODE.findall(_text(GUIDE)))
    assert not undocumented, f"MECHANICAL.md does not document {sorted(undocumented)}"


def _gate_codes() -> list[str]:
    """The module-level code constants of swig_outline_checks (UPPER = 'lower_case')."""
    return sorted({v for k, v in vars(swig_outline_checks).items()
                   if k.isupper() and isinstance(v, str) and re.fullmatch(r"[a-z_]+", v)
                   and k not in ("DEFAULT_FAB", "SEVERITIES")})


def test_gate_codes_are_in_the_guide():
    codes = _gate_codes()
    assert {"courtyard_outside", "copper_outside", "plane_islands"} <= set(codes)
    text = _text(GUIDE)
    missing = [c for c in codes if f"`{c}`" not in text]
    assert not missing, f"MECHANICAL.md does not name the G3/G4 codes {missing}"
    assert e2e_outline_check.AUDIT_CODES == (swig_outline_checks.COURTYARD_OUTSIDE,
                                             swig_outline_checks.PAD_EDGE_CLEARANCE,
                                             swig_outline_checks.COPPER_OUTSIDE)


# --- numbers and names -------------------------------------------------------------------------
def _mm(value: float) -> str:
    return f"{value:g} mm"


FACTS: tuple[tuple[str, Any], ...] = (
    ("DXF join tolerance", _mm(dxf_io.DEFAULT_JOIN_TOL_MM)),
    ("curve tolerance", _mm(dxf_io.DEFAULT_CURVE_TOL_MM)),
    ("Hausdorff tolerance", _mm(mech_export.HAUSDORFF_TOL_MM)),
    ("chord error", _mm(outline_model.CHORD_ERROR_MM)),
    ("DSN margin", _mm(dsn_outline.MARGIN_MM)),
    ("DSN keepout match", _mm(dsn_outline.MATCH_TOL_MM)),
    ("connector reach", _mm(outline_audit.CONNECTOR_MAX_DIST_MM)),
    ("straddle tolerance", _mm(edge_mount.STRADDLE_TOL_MM)),
    ("inscribed margin", _mm(board_field.INSCRIBED_MARGIN_MM)),
    ("hole keepout", _mm(board_field.HOLE_KEEPOUT_MM)),
    ("near edge", _mm(lint_rules_outline.NEAR_EDGE_MM)),
    ("off-board reach", _mm(lint_rules_outline.OFF_BOARD_REACH_MM)),
    ("field clearance", _mm(lint_rules_outline.FIELD_CLEARANCE_MM)),
    ("min field share", f"{lint_rules_outline.MIN_FIELD_SHARE:.0%}"),
    ("sheet margin", _mm(build_features.SHEET_MARGIN_MM)),
    ("e2e area", f"{e2e_outline_check.AREA_TOL_MM2:g} mm^2"),
    ("e2e STEP bbox", _mm(e2e_outline_check.STEP_BBOX_TOL_MM)),
    ("sidecar suffix", outline_resolve.SIDECAR_SUFFIX),
    ("env var", outline_resolve.ENV_VAR),
    ("mech folder", "_mech/"),
    ("DXF layer", dxf_io.DEFAULT_LAYER),
    ("DXF version", dxf_io.DXF_VERSION_OUT),
    ("DXF units", ", ".join(f"`{u}`" for u in dxf_io.DXF_UNITS)),
    ("Fusion units", "$INSUNITS 5"),
    ("Fusion appid", dxf_io.FUSION_APPID),
    ("legacy skip", rp_feature_outline.LEGACY_SKIP),
    ("review section", f"## {review_mechanical.SECTION_TITLE}"),
    ("route hash key", "`outline_fp`"),
    ("grid", "160"),
    ("no contours", "--use-contours"),
    ("STEP flags", "--drill-origin --force"),
    ("STEP board only", "--board-only"),
    ("pos flags", "--use-drill-file-origin --units mm --format csv"),
    ("frame tolerance", _mm(outline.FRAME_TOL_MM)),
    ("chain tolerance", _mm(outline.CHAIN_TOL_MM)),
)


@pytest.mark.parametrize("fact", FACTS, ids=[f[0] for f in FACTS])
def test_guide_quotes_the_code(fact: tuple[str, Any]):
    what, expected = fact
    assert expected in _text(GUIDE), f"MECHANICAL.md does not state the {what} {expected!r}"


def test_dxf_export_flags_match_mech_export():
    text = _text(GUIDE)
    for flag in mech_export.DXF_FLAGS:
        assert " ".join(flag.args) in text, f"MECHANICAL.md lacks the DXF flag {flag.args}"
    assert dxf_io.INSUNITS[5] == ("cm", 10.0)
    assert rp_feature_outline.MECH_SUFFIX == "_mech"
    assert rp_feature_outline.ROUTE_HASH_COMPONENT_KEYS == ("edge",)
    extra = rp_feature_outline.route_hash_extra({"board": {"w": 10, "h": 10, "datum": [0, 0]}},
                                                None)
    assert tuple(extra) == ("outline_fp",)


def test_fab_limits_are_tabulated():
    lim = outline_ops.FAB_LIMITS["JLCPCB"]
    text = _text(GUIDE)
    for rule in lim.sources:
        assert f"`{rule}`" in text, f"MECHANICAL.md lacks the fab rule `{rule}`"
    for value in (lim.min_board_dim, lim.min_slot_width, lim.min_internal_radius,
                  lim.min_feature_width, lim.min_npth_diameter, lim.min_hole_to_edge):
        assert f"{value:g} mm" in text
    assert lim.assumptions == frozenset({"feature_width"})
    assert "ASSUMPTION" in text
    assert list(outline_ops.FAB_LIMITS) == ["JLCPCB"]
    sig = inspect.signature(outline_ops.largest_inscribed_rect)
    assert sig.parameters["grid"].default == 160


def test_default_datum_and_sources_are_stated():
    text = _text(GUIDE)
    assert outline_resolve.SOURCES == ("none", "rect", "rounded_rect", "loop", "dxf")
    for source in outline_resolve.SOURCES:
        assert f"`{source}`" in text, f"MECHANICAL.md lacks the outline source `{source}`"
    assert "bottom-left" in text and "top-left" in text
    assert "tan(theta/4)" in text or "tan(θ/4)" in text
    assert outline_audit.FACING_MIN_COS == pytest.approx(0.7071067811865476)
    assert "45" in text


# --- structure, links and commands ---------------------------------------------------------
def test_guide_sections_in_order():
    headings = re.findall(r"^## (.+)$", _text(GUIDE), re.M)
    found = [next((i for i, h in enumerate(headings) if key.lower() in h.lower()), None)
             for key in SECTIONS]
    missing = [key for key, i in zip(SECTIONS, found, strict=True) if i is None]
    assert not missing, f"MECHANICAL.md has no section for {missing}; headings: {headings}"
    assert found == sorted(found), f"sections out of order: {list(zip(SECTIONS, found))}"


@pytest.mark.parametrize("phrase", ['"at"', '"edge"', "overhang", "PCB Edge", "import_outline_dxf",
                                    "docs/MECHANICAL.md", "corner_radius", '"dxf"', '"loop"',
                                    "datum", "cutouts"])
def test_outline_summary_covers_the_agent_topics(phrase: str):
    assert phrase in _text(SKILL_REF), f"outline.md does not mention {phrase!r}"


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
