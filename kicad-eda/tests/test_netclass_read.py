"""netclass_read — net-class track widths from a .kicad_pro (F5-03a).

The healer, the G4 width gate and the route handoff learn a net's class width
from the project file, never from pcbnew (NETINFO_ITEM.GetNetClass() is an
unwrapped SwigPyObject on KiCad 10 SWIG). The reader resolves a net the way
KiCad does (NET_SETTINGS::GetEffectiveNetClass). The golden answers below were
measured with KiCad 9.0.4 and 10.0.5 (LoadBoard + GetEffectiveNetClass on
these exact projects); the live tier re-measures them on the installed KiCad.

Unit tier (no KiCad):
  * net_class_widths(pro, nets): every matching pattern and netclass_assignment
    contributes, the class order is (priority, name), the first class with a
    track width wins, a Default winner or no width means no class width; the
    combined matcher (an anchored PCRE2 regex whose match starts at the net's
    first character, OR an anchored * / ? wildcard) and pcre_to_python's
    rewrites (POSIX bracket classes, ASCII escapes, \\Z / \\z, \\Q..\\E, inline
    options as scoped groups); the documented gaps; bus patterns and {,n} as
    9.0.4 reads them; the empty net is unclassed; nets is required, so a net's
    answer never depends on which other nets are asked; {} for a missing file;
    ValueError naming the file for malformed JSON or entries; class_width /
    class_of;
  * check_routed_widths reads classes through netclass_read and keeps its
    issue strings byte-identical (fake board, pcbnew stubbed for the import);
  * swig_copper: the helpers moved out of swig_board_fixes keep their names
    there as aliases (the very same function objects).

Live tier (skipped without a KiCad 9/10 bundled Python that imports pcbnew):
  KiCad's own resolution of every scenario equals netclass_read's, every
  documented gap still differs, and `swig_gate_checks.py routed` on a real
  saved board reports the class-width issues through netclass_read.
"""
from __future__ import annotations

import ast
import importlib
import itertools
import json
import subprocess
import sys
import types
import warnings
from pathlib import Path

import pytest

import kicad_env as env
import netclass_read as ncr

TOOLS = Path(__file__).resolve().parents[1] / "server" / "tools"
NM_PER_MM = 1_000_000
LIVE_TIMEOUT_S = 180
INT_MAX = 2**31 - 1
DEFAULT_W = 0.2
DEFAULT = {"name": "Default", "track_width": DEFAULT_W, "clearance": 0.2, "priority": INT_MAX}

BUCK_CLASSES = [DEFAULT,                      # what netclass_writer writes (priority = index)
                {"name": "NC_SW", "track_width": 1.375, "clearance": 0.2, "priority": 0},
                {"name": "NC_V5", "track_width": 1.375, "clearance": 0.2, "priority": 1},
                {"name": "NC_VIN", "track_width": 0.528, "clearance": 0.2, "priority": 2}]
BUCK_PATTERNS = [{"netclass": "NC_VIN", "pattern": "VIN"},
                 {"netclass": "NC_SW", "pattern": "SW"},
                 {"netclass": "NC_V5", "pattern": "V5"}]


def nc(name: str, width: float | None, priority: int | None = None) -> dict:
    """A class entry; width None writes JSON null, priority None omits the key."""
    entry = {"name": name, "track_width": width, "clearance": 0.2}
    return entry if priority is None else {**entry, "priority": priority}


def bind(pattern: str, cls: str) -> dict:
    return {"netclass": cls, "pattern": pattern}


def write_pro(path: Path, classes: list, patterns: list | None,
              assignments: dict | None = None) -> Path:
    """A KiCad 9/10 style .kicad_pro carrying only what the reader looks at."""
    net_settings = {"classes": classes, "meta": {"version": 4}, "net_colors": None,
                    "netclass_assignments": assignments, "netclass_patterns": patterns}
    path.write_text(json.dumps({"board": {}, "net_settings": net_settings}, indent=2),
                    encoding="utf-8")
    return path


@pytest.fixture
def buck_pro(tmp_path: Path) -> Path:
    return write_pro(tmp_path / "buck.kicad_pro", BUCK_CLASSES, BUCK_PATTERNS)


# ---------------------------------------------------------------------------
# Resolution scenarios: KiCad 9.0.4 and 10.0.5 gave exactly these answers.
# expect = {net: (class, width)}; a net left out has no class width.
# ---------------------------------------------------------------------------
SCENARIOS = {
    "buck": dict(classes=BUCK_CLASSES[1:], patterns=BUCK_PATTERNS,
                 nets=["VIN", "SW", "V5", "GND"],
                 expect={"VIN": ("NC_VIN", 0.528), "SW": ("NC_SW", 1.375),
                         "V5": ("NC_V5", 1.375)}),
    "exact_default_does_not_shadow": dict(
        classes=[nc("NC_V", 0.9, 0)], patterns=[bind("V*", "NC_V"), bind("VREF", "Default")],
        nets=["VREF", "VCC"], expect={"VREF": ("NC_V", 0.9), "VCC": ("NC_V", 0.9)}),
    "wildcard_default_does_not_shadow": dict(
        classes=[nc("NC_P", 0.6, 0)], patterns=[bind("VBUS*", "Default"), bind("V*", "NC_P")],
        nets=["VBUS_A", "V3"], expect={"VBUS_A": ("NC_P", 0.6), "V3": ("NC_P", 0.6)}),
    "undefined_class_contributes_nothing": dict(
        classes=[nc("NC_V", 0.9, 0)],
        patterns=[bind("V*", "NC_V"), bind("VX", "NC_UNDEF"), bind("Q2", "NC_UNDEF")],
        nets=["VX", "V1", "Q2"], expect={"VX": ("NC_V", 0.9), "V1": ("NC_V", 0.9)}),
    "explicit_default_alone": dict(
        classes=[], patterns=[bind("GND", "Default")], nets=["GND", "X"], expect={}),
    "priority_beats_an_exact_pattern": dict(
        classes=[nc("NC_A", 0.5, 0), nc("NC_B", 0.7, 1)],
        patterns=[bind("V*", "NC_A"), bind("VIN", "NC_B")],
        nets=["VIN", "V2"], expect={"VIN": ("NC_A", 0.5), "V2": ("NC_A", 0.5)}),
    "priority_not_file_order": dict(
        classes=[nc("NC_A", 0.5, 1), nc("NC_B", 0.7, 0)],
        patterns=[bind("VIN", "NC_B"), bind("V*", "NC_A"), bind("VIN", "NC_A")],
        nets=["VIN", "V2"], expect={"VIN": ("NC_B", 0.7), "V2": ("NC_A", 0.5)}),
    "two_wildcards_by_priority": dict(
        classes=[nc("NC_A", 0.5, 1), nc("NC_B", 0.7, 0)],
        patterns=[bind("VBUS*", "NC_A"), bind("V*", "NC_B")],
        nets=["VBUS_A", "V3"], expect={"VBUS_A": ("NC_B", 0.7), "V3": ("NC_B", 0.7)}),
    "equal_priority_by_case_sensitive_name": dict(
        classes=[nc("NC_B", 0.7, 0), nc("nc_a", 0.45, 0), nc("NC_A", 0.5, 0)],
        patterns=[bind("VIN", "NC_B"), bind("V*", "nc_a"), bind("VI*", "NC_A")],
        nets=["VIN", "V2", "VIX"],
        expect={"VIN": ("NC_A", 0.5), "V2": ("nc_a", 0.45), "VIX": ("NC_A", 0.5)}),
    "widthless_classes_fall_through": dict(
        classes=[nc("NC_N", None, 0), nc("NC_W", 0.75, 1),
                 {"name": "NC_M", "clearance": 0.3, "priority": 2}],
        patterns=[bind("A*", "NC_N"), bind("AB", "NC_W"), bind("M1", "NC_M")],
        nets=["AB", "AC", "M1"], expect={"AB": ("NC_W", 0.75)}),
    "low_priority_default_wins_when_bound": dict(
        default={**DEFAULT, "track_width": 0.33, "priority": 0},
        classes=[nc("NC_A", 0.5, 5)],
        patterns=[bind("A1", "NC_A"), bind("A1", "Default"), bind("A2", "NC_A")],
        nets=["A1", "A2"], expect={"A2": ("NC_A", 0.5)}),
    "netclass_assignments_contribute": dict(
        classes=[nc("NC_A", 0.5, 1), nc("NC_B", 0.7, 0)], patterns=[bind("D*", "NC_A")],
        assignments={"D1": ["NC_B"], "Q1": ["NC_A"], "Q2": ["NC_UNDEF"]},
        nets=["D1", "D2", "Q1", "Q2"],
        expect={"D1": ("NC_B", 0.7), "D2": ("NC_A", 0.5), "Q1": ("NC_A", 0.5)}),
    "empty_net_is_unclassed": dict(
        classes=[nc("NC_ALL", 0.4, 0)], patterns=[bind("*", "NC_ALL")],
        nets=["", "A"], expect={"A": ("NC_ALL", 0.4)}),
    "missing_priority_sorts_first": dict(          # KiCad 10 only (9.0.4 drops the widths)
        kicad_min=10, classes=[nc("NC_Z", 0.5), nc("NC_B", 0.7, 0), nc("NC_Y", 0.65)],
        patterns=[bind("V*", "NC_Z"), bind("VIN", "NC_B"), bind("W*", "NC_Y"),
                  bind("W1", "NC_B")],
        nets=["VIN", "W1"], expect={"VIN": ("NC_Z", 0.5), "W1": ("NC_Y", 0.65)}),
}

# (pattern, net, KiCad matches?) — KiCad's netclass matcher is an anchored
# regex (a match must start at the net's first character) OR an anchored * / ?
# wildcard; every row measured identically on 9.0.4 and 10.0.5.
MATCHES = [
    ("VIN", "VIN", True), ("VIN", "VINX", False), ("vin", "VIN", False), ("vin", "vin", True),
    ("VBUS*", "VBUS_A", True), ("VBUS*", "VBU", True), ("VBUS*", "VBUSSS", True),
    ("VBUS*", "XVBUS", False), ("D?", "D", True), ("D?", "D1", True), ("D?", "D10", False),
    ("v*", "VIN", False), ("v*", "vin", True),
    ("in\\d+", "in12", True), ("in\\d+", "in", False), ("in\\d+", "ind", False),
    ("B[0]", "B0", True), ("B[0]", "B[0]", True), ("B[0]", "B1", False),
    ("+3.3V", "+3.3V", True), ("+3.3V", "+3x3V", False), ("+3.3V", "+3.3VX", False),
    ("+3.3V", "A+3.3V", False),
    ("(VIN", "(VIN", True), ("(VIN", "(VINX", False), ("(VIN", "VIN", False),
    ("VIN$", "VIN", True), ("VIN$", "AVIN", False), ("VIN$", "VINX", False),
    # '.' matches a newline; '$' matches at the end or before one final newline
    ("VIN$", "VIN\n", True), ("VIN$", "VIN\n\n", False), ("VIN$", "VIN\r", False),
    ("V.N", "V\nN", True), ("V*N", "VIN\n", True), ("V*N", "V\nIN", True),
    ("VIN.", "VIN\n\n", True), ("VIN.", "VINX", True),
    ("A.B", "A.B", True), ("A.B", "AxB", True),
    ("^C[0-9]$", "C1", True), ("^C[0-9]$", "C12", False),
    ("/USB/D+", "/USB/D", True), ("/USB/D+", "/USB/DD", True), ("/USB/D+", "/USB/D+", True),
    ("/USB/D+", "/USB/D-", False),
    # top-level alternation: '^X|Y$' is '^X' or 'Y$', and only a match at 0 counts
    ("X|Y", "X", True), ("X|Y", "Y", True), ("X|Y", "X|Y", True), ("X|Y", "XY", True),
    ("X|Y", "XA", True), ("X|Y", "AY", False), ("X|Y", "YA", False), ("X|Y", "AXA", False),
    ("VIN|SW", "VINX", True), ("VIN|SW", "VINSW", True), ("VIN|SW", "XSW", False),
    ("VIN|SW", "AVIN", False), ("(VIN|SW)", "SW", True), ("(VIN|SW)", "VINX", False),
    ("^A|B", "AZ", True), ("^A|B", "B", True), ("^A|B", "ZB", False), ("^A|B", "BZ", False),
    # ARE bracket expressions: POSIX classes (ASCII only), ']' first is literal
    ("N[[:digit:]]", "N1", True), ("N[[:digit:]]", "N:]", False), ("N[[:digit:]]", "Nd]", False),
    ("N[[:digit:]]", "N12", False), ("N[^[:digit:]]", "Nx", True), ("N[^[:digit:]]", "N1", False),
    ("[[:upper:]][[:alnum:]_]*", "VIN_3", True), ("[[:upper:]][[:alnum:]_]*", "vIN", False),
    ("[[:upper:]][[:alnum:]_]*", "V-1", False),
    ("N[[:alpha:]]", "NZ", True), ("N[[:alpha:]]", "N1", False), ("N[[:alpha:]]", "Né", False),
    ("N[[:xdigit:]]", "Na", True), ("N[[:xdigit:]]", "NG", False),
    ("N[[:punct:]]", "N_", True), ("N[[:punct:]]", "N+", True), ("N[[:punct:]]", "Na", False),
    ("N[[:space:][:digit:]]", "N ", True), ("N[[:space:][:digit:]]", "Nx", False),
    ("N[a-c[:digit:]]", "N5", True), ("N[a-c[:digit:]]", "Nd", False),
    ("N[]x]", "N]", True), ("N[^]x]", "N]", False), ("N[^]x]", "Ny", True),
    # an unknown class, a collating element or an equivalence class: no regex at all
    ("N[[:foo:]]", "N[[:foo:]]", True), ("N[[:foo:]]", "Nf]", False),
    ("N[[.a.]]", "N[[.a.]]", True), ("N[[.a.]]", "Na]", False), ("N[[.a.]]", "Na", False),
    ("N[[=a=]]", "N[[=a=]]", True), ("N[[=a=]]", "Na]", False),
    ("a&&b", "a&&b", True), ("a&&b", "a&b", False),
    # an invalid regex is dropped, never matched as a substring of '^<pattern>$'
    ("N[[:foo:]]", "^N[[:foo:]]$", False), ("N[[:foo:]]", "N[[:foo:]]X", False),
    # a trailing '\$' counts as the '$' anchor, so no '$' is appended
    ("A\\$", "A$", True), ("A\\$", "A$X", True), ("A\\$", "A", False), ("A\\$", "A\\$", True),
    # PCRE2, as wxRegEx compiles it: an inline option lasts to the end of its
    # group and carries into the group's later '|' branches
    ("(?i)vin", "VIN", True), ("(?i)vin", "Vin", True), ("(?i)vin", "(Xi)vin", True),
    ("V(?i)in", "VIN", True), ("V(?i)in", "vIN", False),
    ("(V(?i)i)n", "VIn", True), ("(V(?i)i)n", "VIN", False),
    ("(?-i)VIN", "VIN", True), ("(?-i)VIN", "vin", False),
    ("(?i)V(?-i)IN", "vIN", True), ("(?i)V(?-i)IN", "vin", False),
    ("(a(?i)b|c)", "aB", True), ("(a(?i)b|c)", "C", True), ("(a(?i)b|c)", "Ab", False),
    ("(a(?i)b|c)", "ac", False),
    ("a(?i)b|c", "aBx", True), ("a(?i)b|c", "C", True), ("a(?i)b|c", "xC", False),
    ("a(?i)b|c", "AB", False),
    ("(?i)a|b", "B", True), ("(?i)a|b", "Ax", True), ("(?i)a|b", "xB", False),
    ("(?i:v)IN", "vIN", True), ("(?i:v)IN", "vin", False), ("(?i:a)|b", "B", False),
    ("(?s)V.N", "VIN", True), ("(?-s)A.B", "A\nB", False), ("(?-s)A.B", "AxB", True),
    ("(?m)A$", "A\nB", True), ("(?x)V I N", "VIN", True), ("(?x)V I N", "V I N", False),
    ("(?x)a b|c d", "cd", True), ("(?i)Né", "NÉ", True),
    # no UCP: \d \w \s \b and their negations are ASCII-only, in and out of sets
    ("in\\d+", "in١٢", False), ("A\\DB", "A١B", True), ("A\\DB", "A1B", False),
    ("N\\w", "Né", False), ("N\\w", "N_", True), ("A\\WB", "AéB", True),
    ("A\\sB", "A B", False), ("A\\sB", "A\x0bB", True), ("A\\SB", "A B", True),
    ("Né\\b", "Né", False), ("N\\b", "N", True), ("N\\Bé", "Né", False),
    ("[\\d]X", "١X", False), ("[\\d]X", "1X", True), ("[\\w]X", "éX", False),
    ("[\\s]X", " X", False), ("[\\s]X", " X", True),
    # \Z allows a final newline, \z does not; \Q..\E quotes (without \E up to the
    # end, the appended '$' included); a stray \E is ignored
    ("A\\Z", "A\n", True), ("A\\z", "A", True), ("A\\z", "A\n", False),
    ("\\QA.B\\E", "A.B", True), ("\\QA.B\\E", "AxB", False),
    ("\\QA.B", "A.B", False), ("\\QA.B", "A.B$X", True),
    ("(?i)A\\QB.C\\E", "ab.c", True), ("(?i)A\\QB.C\\E", "abxc", False),
    ("A\\EB", "AB", True), ("A\\EB", "A\\EB", True),
    ("(?#c)AB", "AB", True), ("(?#a[b)X", "X", True), ("A++B", "AAB", True),
    ("(?>A+)B", "AAB", True), ("(A)\\1", "AA", True), ("(A)\\1", "AB", False),
    # KiCad's own net-name shapes (overbar, unnamed net, subscript)
    ("~{RST}", "~{RST}", True), ("~{RST}", "~RST", False),
    ("Net-(U1-Pad3)", "Net-(U1-Pad3)", True), ("Net-(U1-Pad3)", "Net-U1-Pad3", True),
    ("Net-(U1-Pad3)", "Net-(U1-Pad3)X", False),
    ("D_{0}", "D", True), ("D_{0}", "D_{0}", True), ("D_{0}", "D_0", False),
    ("*", "ANY", True),
]

# KiCad 9.0.4 answers that 10.0.5 does not share; netclass_read follows 9.0.4.
# Patterns that parse as a KiCad bus (vector D[0..3], group X{A B}): 9.0.4
# matches them as regex / wildcard; 10.0.5 expands them into member names first
# and differs on AA, D1, D. and A. The open quantifier {,n}: 9.0.4 (and Python)
# read {0,n}; 10.0.5 reads it literally.
KICAD9_MATCHES = [
    ("A{2}", "AA", True), ("A{2}", "A{2}", True), ("A{2}", "A", False),
    ("D[0..3]", "D0", True), ("D[0..3]", "D1", False), ("D[0..3]", "D.", True),
    ("D[0..3]", "D4", False), ("X{A B}", "X{A B}", True), ("X{A B}", "A", False),
    ("A{,2}B", "B", True), ("A{,2}B", "AAB", True), ("A{,2}B", "A{,2}B", True),
]

# Documented gaps: PCRE2-only syntax that Python's re rejects (netclass_read then
# has only the wildcard reading) or reads otherwise (\v), so netclass_read answers
# the OPPOSITE of KiCad (these rows hold KiCad's answer; the live tier checks
# that KiCad still gives it and that netclass_read still differs).
KNOWN_GAPS = [   # (pattern, net, KiCad matches?, where the docstring lists it)
    ("A\\hB", "A B", True, "\\h"), ("A\\R", "A\r\n", True, "\\R"), ("A\\vB", "A\nB", True, "\\v"),
    ("\\pL+", "ABC", True, "\\p{..}"), ("(?<n>A)B", "AB", True, "(?<name>..)"),
    ("(?|(A)|(B))", "B", True, "(?|..)"),
]
NC_R = ("NC_R", 0.45)


def _scenario_pro(tmp_path: Path, name: str, sc: dict) -> Path:
    return write_pro(tmp_path / f"{name}.kicad_pro", [sc.get("default", DEFAULT)] + sc["classes"],
                     sc["patterns"], sc.get("assignments"))


def _matcher_scenarios() -> dict:
    """One project per MATCHES / KICAD9_MATCHES / KNOWN_GAPS pattern, binding
    NC_R (0.45 mm); expect holds KiCad's answer."""
    out = {}
    for prefix, rows_all, extra in (("match", MATCHES, {}),
                                    ("k9", KICAD9_MATCHES, {"kicad_max": 9}),
                                    ("gap", KNOWN_GAPS, {"gap": True})):
        for i, (pattern, rows) in enumerate(itertools.groupby(rows_all, key=lambda r: r[0])):
            rows = list(rows)
            out[f"{prefix}_{i}"] = dict(
                classes=[nc(NC_R[0], NC_R[1], 0)], patterns=[bind(pattern, NC_R[0])],
                nets=[row[1] for row in rows],
                expect={row[1]: NC_R for row in rows if row[2]}, **extra)
    return out


# ---------------------------------------------------------------------------
# net_class_widths / class_width / class_of / pattern_matches
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_resolution_matches_kicad(tmp_path: Path, name: str):
    sc = SCENARIOS[name]
    pro = _scenario_pro(tmp_path, name, sc)
    assert ncr.net_class_widths(pro, sc["nets"]) == sc["expect"]


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_a_nets_answer_does_not_depend_on_the_other_nets_asked(tmp_path: Path, name: str):
    sc = SCENARIOS[name]
    pro = _scenario_pro(tmp_path, name, sc)
    together = ncr.net_class_widths(pro, sc["nets"])
    for net in sc["nets"]:
        alone = ncr.net_class_widths(pro, [net])
        assert alone == ({net: together[net]} if net in together else {}), net
        assert ncr.class_of(together, net) == together.get(net)


@pytest.mark.parametrize("name", ["priority_not_file_order", "netclass_assignments_contribute",
                                  "equal_priority_by_case_sensitive_name"])
def test_file_order_of_classes_and_patterns_is_irrelevant(tmp_path: Path, name: str):
    sc = SCENARIOS[name]
    first = ncr.net_class_widths(_scenario_pro(tmp_path, "a", sc), sc["nets"])
    rev = {**sc, "classes": sc["classes"][::-1], "patterns": sc["patterns"][::-1]}
    assert ncr.net_class_widths(_scenario_pro(tmp_path, "b", rev), sc["nets"][::-1]) == first


@pytest.mark.parametrize("pattern,net,expected", MATCHES + KICAD9_MATCHES)
def test_pattern_matches_like_kicad(pattern: str, net: str, expected: bool):
    assert ncr.pattern_matches(pattern, net) is expected


@pytest.mark.parametrize("pattern,net,kicad,doc_token", KNOWN_GAPS)
def test_known_gaps_are_documented_and_still_differ(pattern: str, net: str, kicad: bool,
                                                    doc_token: str):
    assert ncr.pattern_matches(pattern, net) is not kicad
    doc = ncr.__doc__ or ""
    gaps = doc[doc.index("Known differences"):]
    assert f"``{doc_token}``" in gaps, doc_token


D, W, S = "0-9", "0-9A-Za-z_", "\\t\\n\\x0b\\f\\r "


@pytest.mark.parametrize("pcre,python", [
    ("^N[[:digit:]]$", "^N[0-9]$"), ("^N[^[:upper:][:digit:]_]$", "^N[^A-Z0-9_]$"),
    ("^N[]x]$", "^N[\\]x]$"), ("^N[^]x]$", "^N[^\\]x]$"), ("^N[[x&|~]$", "^N[\\[x\\&\\|\\~]$"),
    ("^A\\[B\\]\\$$", "^A\\[B\\]\\$$"), ("^N[\\]]$", "^N[\\]]$"),
    ("^N[[:foo:]]$", None), ("^N[[:digit]]$", None), ("^N[[.a.]]$", None),
    ("^N[[=a=]]$", None), ("^N[x$", None),
    # inline options become scoped groups, closed with their group, reopened per branch
    ("^(?i)vin$", "^(?i:vin$)"), ("^(a(?i)b|c)$", "^(a(?i:b)|(?i:c))$"),
    ("^a(?i)b|c$", "^a(?i:b)|(?i:c$)"), ("^(?i)V(?-i)IN$", "^(?i:V(?-i:IN$))"),
    ("^(?i:a)|b$", "^(?i:a)|b$"), ("^(?:a|b)$", "^(?:a|b)$"), ("^(?#a[b)X$", "^(?#a[b)X$"),
    # ASCII escapes, \Z / \z, \Q..\E, stray \E
    ("^A\\dB$", f"^A[{D}]B$"), ("^[\\d\\w\\s]$", f"^[{D}{W}{S}]$"),
    ("^A\\D\\W\\S$", f"^A[^{D}][^{W}][^{S}]$"),
    ("^A\\Z$", "^A(?=\\n?\\Z)$"), ("^A\\z$", "^A\\Z$"), ("^\\QA.B\\E$", "^A\\.B$"),
    ("^\\QA.B$", "^A\\.B\\$"), ("^A\\EB$", "^AB$"), ("^A\\1$", "^A\\1$"),
])
def test_pcre_text_becomes_python_re_text(pcre: str, python: str | None):
    assert ncr.pcre_to_python(pcre) == python


def test_word_boundaries_are_ascii():
    text = ncr.pcre_to_python("\\b\\B")
    assert text is not None and "\\b" not in text and "\\B" not in text and f"[{W}]" in text


@pytest.mark.parametrize("pattern", ["W[[:digit:]]", "W[[x]", "W[a&&b]", "W[a--b]", "W[a||b]"])
def test_pattern_matching_never_warns(pattern: str):
    """Python's re warns (FutureWarning, 'Possible nested set') on '[[', '&&',
    '--' and '||' inside a set; KiCad children must keep stderr clean."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        ncr.pattern_matches(pattern, "W1")


def test_buck_classes_and_lookups(buck_pro: Path):
    widths = ncr.net_class_widths(buck_pro, ["VIN", "SW", "GND"])
    assert widths == {"SW": ("NC_SW", 1.375), "VIN": ("NC_VIN", 0.528)}
    assert list(widths) == ["SW", "VIN"]                     # sorted, whatever the input order
    assert ncr.class_width(widths, "VIN") == 0.528
    assert ncr.class_width(widths, "GND") is None
    assert ncr.class_of(widths, "SW") == ("NC_SW", 1.375)
    assert ncr.class_of(widths, "GND") is None


def test_nets_are_required_and_must_not_be_one_string(buck_pro: Path):
    with pytest.raises(TypeError):
        ncr.net_class_widths(buck_pro)  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="net names"):
        ncr.net_class_widths(buck_pro, "VIN")


def test_nets_may_be_any_iterable_with_repeats(buck_pro: Path):
    expect = {"SW": ("NC_SW", 1.375), "VIN": ("NC_VIN", 0.528)}
    assert ncr.net_class_widths(str(buck_pro), {"VIN", "SW"}) == expect
    assert ncr.net_class_widths(buck_pro, iter(["VIN", "SW", "VIN"])) == expect


def test_width_is_a_float_and_priority_may_be_integral_float(tmp_path: Path):
    pro = write_pro(tmp_path / "f.kicad_pro",
                    [DEFAULT, {"name": "NC_I", "track_width": 1, "priority": 2.0},
                     nc("NC_J", 0.7, 1)],
                    [bind("N", "NC_I"), bind("N*", "NC_J"), bind("M", "NC_I")])
    got = ncr.net_class_widths(pro, ["N", "M"])
    assert got == {"M": ("NC_I", 1.0), "N": ("NC_J", 0.7)}
    assert isinstance(got["M"][1], float)


def test_an_assignment_may_name_a_single_class(tmp_path: Path):
    """KiCad 9/10 write a list of class names; one bare name is read as [name]."""
    pro = write_pro(tmp_path / "one.kicad_pro", [DEFAULT, nc("NC_A", 0.5, 0)], [],
                    {"/USB/D+": "NC_A"})
    assert ncr.net_class_widths(pro, ["/USB/D+", "/USB/D-"]) == {"/USB/D+": ("NC_A", 0.5)}


@pytest.mark.parametrize("patterns", [None, []])
def test_no_patterns_means_no_classed_nets(tmp_path: Path, patterns):
    pro = write_pro(tmp_path / "n.kicad_pro", BUCK_CLASSES, patterns)
    assert ncr.net_class_widths(pro, ["VIN", "SW"]) == {}


def test_project_without_net_settings(tmp_path: Path):
    pro = tmp_path / "bare.kicad_pro"
    pro.write_text(json.dumps({"board": {}}), encoding="utf-8")
    assert ncr.net_class_widths(pro, ["VIN"]) == {}


def test_missing_file_is_empty(tmp_path: Path):
    assert ncr.net_class_widths(tmp_path / "nope.kicad_pro", ["VIN"]) == {}
    assert ncr.net_class_widths(tmp_path, ["VIN"]) == {}     # a directory is not a project


def _ns(**fields) -> str:
    return json.dumps({"net_settings": fields})


@pytest.mark.parametrize("text", [
    "{not json", "[1, 2]", '{"net_settings": [1]}', _ns(classes=3), _ns(netclass_patterns={}),
    _ns(classes=["not a class"]), _ns(classes=[{"track_width": 0.3}]),
    _ns(classes=[{"name": "A", "track_width": "0.3"}]),
    _ns(classes=[{"name": "A", "track_width": True}]),
    _ns(classes=[{"name": "A", "track_width": 0}]),
    _ns(classes=[{"name": "A", "track_width": 0.3, "priority": "1"}]),
    _ns(classes=[{"name": "A", "track_width": 0.3, "priority": False}]),
    _ns(netclass_patterns=["junk"]), _ns(netclass_patterns=[{"pattern": "X"}]),
    _ns(netclass_patterns=[{"pattern": 3, "netclass": "A"}]),
    _ns(netclass_assignments=[]), _ns(netclass_assignments={"X": 3}),
    _ns(netclass_assignments={"X": ["A", 3]}),
])
def test_malformed_project_raises_naming_the_file(tmp_path: Path, text: str):
    pro = tmp_path / "broken.kicad_pro"
    pro.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=r"broken\.kicad_pro"):
        ncr.net_class_widths(pro, ["X"])


def test_undecodable_project_raises_naming_the_file(tmp_path: Path):
    pro = tmp_path / "latin.kicad_pro"
    pro.write_bytes(b'{"net_settings": "\xff\xfe"}')
    with pytest.raises(ValueError, match=r"latin\.kicad_pro"):
        ncr.net_class_widths(pro, ["X"])


def test_result_survives_a_json_round_trip(buck_pro: Path):
    widths = json.loads(json.dumps(ncr.net_class_widths(buck_pro, ["VIN", "SW"])))
    assert ncr.class_width(widths, "SW") == 1.375
    assert ncr.class_of(widths, "VIN") == ("NC_VIN", 0.528)


def test_netclass_read_is_stdlib_only():
    """It is imported by KiCad's bundled Python (stdlib + pcbnew only)."""
    tree = ast.parse((TOOLS / "netclass_read.py").read_text(encoding="utf-8"))
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
            for a in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree)
             if isinstance(n, ast.ImportFrom) and n.module}
    assert mods <= set(sys.stdlib_module_names) | {"__future__"}, mods


# ---------------------------------------------------------------------------
# Consumers: check_routed_widths and the swig_copper move (pcbnew stubbed)
# ---------------------------------------------------------------------------
def _import_with_stub(name: str) -> types.ModuleType:
    """Import a pcbnew-using tools module; stub pcbnew for that import only."""
    try:
        import pcbnew  # noqa: F401  (KiCad's bundled python only)
    except ImportError:
        pass
    else:
        return importlib.import_module(name)
    stub = types.ModuleType("pcbnew")
    stub.ToMM = lambda nm: nm / NM_PER_MM
    stub.FromMM = lambda mm: int(round(mm * NM_PER_MM))
    stub.F_CrtYd, stub.B_CrtYd, stub.F_Cu, stub.B_Cu = 46, 47, 0, 2
    sys.modules["pcbnew"] = stub
    try:
        return importlib.import_module(name)
    finally:
        if sys.modules.get("pcbnew") is stub:
            del sys.modules["pcbnew"]


class Vec:
    def __init__(self, x_mm: float, y_mm: float) -> None:
        self.x, self.y = int(x_mm * NM_PER_MM), int(y_mm * NM_PER_MM)


class Track:
    def __init__(self, net: str, a: tuple, b: tuple, width: float, code: int) -> None:
        self._net, self._a, self._b, self._w, self._code = net, a, b, width, code

    def GetClass(self) -> str:  # noqa: N802 (pcbnew API)
        return "PCB_TRACK"

    def GetNetname(self) -> str:  # noqa: N802
        return self._net

    def GetNetCode(self) -> int:  # noqa: N802
        return self._code

    def GetWidth(self) -> int:  # noqa: N802
        return int(self._w * NM_PER_MM)

    def GetStart(self) -> Vec:  # noqa: N802
        return Vec(*self._a)

    def GetEnd(self) -> Vec:  # noqa: N802
        return Vec(*self._b)

    def GetLength(self) -> int:  # noqa: N802
        return int(((self._a[0] - self._b[0]) ** 2
                    + (self._a[1] - self._b[1]) ** 2) ** 0.5 * NM_PER_MM)


class Board:
    def __init__(self, tracks: list) -> None:
        self._tracks = tracks

    def GetTracks(self) -> list:  # noqa: N802
        return list(self._tracks)

    def GetFootprints(self) -> list:  # noqa: N802
        return []


def _open_field_board() -> Board:
    """Every track far from any pad or via: no entry-neck exemption applies."""
    return Board([Track("VIN", (10, 10), (30, 10), 0.3, 1),
                  Track("SW", (10, 20), (30, 20), 1.375, 2),
                  Track("VBUS_A", (10, 30), (30, 30), 0.3, 3),
                  Track("GND", (10, 40), (30, 40), 0.1, 4)])


def test_check_routed_widths_issue_strings_are_unchanged(buck_pro: Path):
    gc = _import_with_stub("swig_gate_checks")
    issues = gc.check_routed_widths(_open_field_board(), str(buck_pro), {})
    assert issues == ["VIN: open-field segment routed at 0.300 mm but class NC_VIN "
                      "requires 0.528 mm"]


def test_check_routed_widths_keeps_the_preroute_class_label(buck_pro: Path):
    gc = _import_with_stub("swig_gate_checks")
    design = {"preroutes": [{"net": "VIN", "width": 1.0,
                             "path": [["xy", 0, 0], ["xy", 10, 0]]}]}
    issues = gc.check_routed_widths(_open_field_board(), str(buck_pro), design)
    assert issues == [
        "VIN: prerouted 1.0 mm power path missing from the routed board (0 mm of fat "
        "copper survives, authored 10.0 mm — router removed it?)",
        "VIN: open-field segment routed at 0.300 mm but class preroute requires 0.528 mm"]


def test_check_routed_widths_without_a_project_checks_nothing(tmp_path: Path):
    gc = _import_with_stub("swig_gate_checks")
    assert gc.check_routed_widths(_open_field_board(), str(tmp_path / "x.kicad_pro")) == []


def test_check_routed_widths_resolves_classes_like_kicad(tmp_path: Path):
    """A wildcard class now reaches VBUS_A; GND bound only to Default has no
    class width (KiCad: Default) and is not checked."""
    gc = _import_with_stub("swig_gate_checks")
    pro = write_pro(tmp_path / "w.kicad_pro", [DEFAULT, nc("NC_VBUS", 0.8, 0)],
                    [bind("VBUS*", "NC_VBUS"), bind("GND", "Default")])
    assert gc.check_routed_widths(_open_field_board(), str(pro)) == [
        "VBUS_A: open-field segment routed at 0.300 mm but class NC_VBUS requires 0.800 mm"]


def test_check_routed_widths_never_classes_netless_copper(tmp_path: Path):
    gc = _import_with_stub("swig_gate_checks")
    pro = write_pro(tmp_path / "s.kicad_pro", [DEFAULT, nc("NC_ALL", 0.4, 0)],
                    [bind("*", "NC_ALL")])
    board = Board([Track("", (10, 10), (30, 10), 0.1, 0),
                   Track("A", (10, 20), (30, 20), 0.5, 1),
                   Track("B", (10, 30), (30, 30), 0.3, 2)])
    assert gc.check_routed_widths(board, str(pro)) == [
        "B: open-field segment routed at 0.300 mm but class NC_ALL requires 0.400 mm"]


MOVED = ("to", "mm", "dist", "min_clearance", "via_w", "net_tracks", "attach_points",
         "via_template", "add_via", "seg_dist", "clear_spot", "clear_path",
         "nearest_net_via", "net_vias_near", "bfs_path")


def test_swig_copper_helpers_keep_their_old_names_in_swig_board_fixes():
    fixes = _import_with_stub("swig_board_fixes")
    copper = importlib.import_module("swig_copper")
    for name in MOVED:
        assert getattr(fixes, "_" + name) is getattr(copper, name), name
        assert "_" + name not in vars(copper), f"swig_copper still defines _{name}"


def test_swig_copper_pure_geometry():
    copper = _import_with_stub("swig_copper")
    assert copper.dist((0, 0), (3, 4)) == 5.0
    assert copper.seg_dist((5, 3), (0, 0), (10, 0)) == 3.0
    assert copper.seg_dist((-4, 3), (0, 0), (10, 0)) == 5.0
    assert copper.seg_dist((1, 1), (2, 2), (2, 2)) == pytest.approx(2 ** 0.5)


# ---------------------------------------------------------------------------
# Live tier: KiCad's own Python
# ---------------------------------------------------------------------------
BUILD_BOARD = '''\
import sys
sys.path.insert(0, sys.argv[1])
import kicad_quiet  # noqa: E402,F401  (before pcbnew)
import pcbnew  # noqa: E402
import netclass_read, swig_copper, swig_board_fixes, swig_gate_checks  # noqa: E402,F401

board = pcbnew.CreateEmptyBoard()
for code, (name, y, w) in enumerate((("VIN", 10, 0.3), ("SW", 20, 1.375),
                                      ("VBUS_A", 30, 0.3), ("GND", 40, 0.1)), 1):
    net = pcbnew.NETINFO_ITEM(board, name)
    board.Add(net)
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(10), pcbnew.FromMM(y)))
    t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(30), pcbnew.FromMM(y)))
    t.SetWidth(pcbnew.FromMM(w))
    t.SetLayer(pcbnew.F_Cu)
    t.SetNet(net)
    board.Add(t)
pcbnew.SaveBoard(sys.argv[2], board)
print("built", pcbnew.Version())
'''

# Saves one empty board per name; a SECOND process must load them (a process
# that saved a board keeps that project in memory and ignores later edits).
SAVE_EMPTY_BOARDS = '''\
import sys
sys.path.insert(0, sys.argv[1])
import kicad_quiet  # noqa: E402,F401  (before pcbnew)
import pcbnew  # noqa: E402

for pcb in sys.argv[2:]:
    pcbnew.SaveBoard(pcb, pcbnew.CreateEmptyBoard())
print("saved", pcbnew.Version())
'''

KICAD_EFFECTIVE_CLASSES = '''\
import json, sys
sys.path.insert(0, sys.argv[1])
import kicad_quiet  # noqa: E402,F401  (before pcbnew)
import pcbnew  # noqa: E402

asks = json.loads(open(sys.argv[2], encoding="utf-8").read())
out = {}
for pcb, nets in asks.items():
    board = pcbnew.LoadBoard(pcb)
    if board is None:
        raise SystemExit("LoadBoard failed: " + pcb)
    settings = board.GetDesignSettings().m_NetSettings
    settings.ClearAllCaches()
    out[pcb] = {}
    for net in nets:
        cls = settings.GetEffectiveNetClass(net)
        out[pcb][net] = [cls.GetName(), pcbnew.ToMM(cls.GetTrackWidth())]
print(json.dumps({"version": pcbnew.Version(), "classes": out}))
'''


@pytest.fixture(scope="module")
def kicad_swig_python() -> tuple[Path, tuple[int, int, int]]:
    """A KiCad 9/10 bundled Python verified (by kicad_env) to import pcbnew."""
    found = sorted((inst for inst in env.discover()
                    if inst.version_tuple[0] in (9, 10) and inst.python is not None),
                   key=lambda inst: inst.version_tuple)
    if not found:
        pytest.skip("no KiCad 9/10 bundled Python with pcbnew")
    return found[0].python, found[0].version_tuple


def _run_live(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", stdin=subprocess.DEVNULL,
                              timeout=LIVE_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        pytest.fail(f"KiCad child hung for {LIVE_TIMEOUT_S} s: {e.stdout!r} {e.stderr!r}")


def _run_script(python: Path, tmp_path: Path, name: str, script: str,
                args: list[str]) -> str:
    child = tmp_path / name
    child.write_text(script, encoding="utf-8")
    done = _run_live([str(python), str(child), str(TOOLS), *args])
    assert done.returncode == 0, (done.stdout, done.stderr)
    return done.stdout.strip().splitlines()[-1]


def test_live_kicad_resolves_every_scenario_like_netclass_read(kicad_swig_python, tmp_path):
    python, version = kicad_swig_python
    scenarios = {name: sc for name, sc in {**SCENARIOS, **_matcher_scenarios()}.items()
                 if sc.get("kicad_min", 9) <= version[0] <= sc.get("kicad_max", 10)}
    pcbs = {name: tmp_path / f"{name}.kicad_pcb" for name in scenarios}
    _run_script(python, tmp_path, "save.py", SAVE_EMPTY_BOARDS, [str(p) for p in pcbs.values()])
    for name, sc in scenarios.items():                  # replaces the project SaveBoard wrote
        _scenario_pro(tmp_path, name, sc)
    asks = tmp_path / "asks.json"
    asks.write_text(json.dumps({str(pcbs[n]): sc["nets"] for n, sc in scenarios.items()}),
                    encoding="utf-8")
    kicad = json.loads(_run_script(python, tmp_path, "classes.py", KICAD_EFFECTIVE_CLASSES,
                                   [str(asks)]))["classes"]
    for name, sc in scenarios.items():
        ours = ncr.net_class_widths(pcbs[name].with_suffix(".kicad_pro"), sc["nets"])
        reference = sc["expect"] if sc.get("gap") else ours
        _assert_kicad_gives(name, kicad[str(pcbs[name])], reference,
                            sc.get("default", DEFAULT)["track_width"])
        if sc.get("gap"):                               # a documented gap still differs
            assert all((n in ours) != (n in reference) for n in sc["nets"]), (name, ours)
        else:
            assert ours == sc["expect"], name


def _assert_kicad_gives(name: str, kicad_rows: dict, reference: dict, default_w: float) -> None:
    """KiCad's [class name, width] per net equals ``reference`` (a
    net_class_widths-shaped answer; a net left out routes at default_w)."""
    for net, (kicad_name, kicad_w) in kicad_rows.items():
        want = reference[net][1] if net in reference else default_w
        assert kicad_w == pytest.approx(want), (name, net, kicad_name, reference.get(net))
        if net in reference:
            assert reference[net][0] in kicad_name.split(","), (name, net, kicad_name)


def test_live_routed_gate_reads_classes_through_netclass_read(kicad_swig_python, tmp_path):
    python, _version = kicad_swig_python
    pcb = tmp_path / "live.kicad_pcb"
    built = _run_script(python, tmp_path, "build.py", BUILD_BOARD, [str(pcb)])
    assert built.startswith("built"), built
    write_pro(pcb.with_suffix(".kicad_pro"),          # after SaveBoard wrote its own
              BUCK_CLASSES + [nc("NC_VBUS", 0.8, 3)],
              BUCK_PATTERNS + [bind("VBUS*", "NC_VBUS"), bind("GND", "Default")])
    gate = _run_live([str(python), str(TOOLS / "swig_gate_checks.py"), "routed", str(pcb)])
    assert gate.returncode == 0, (gate.stdout, gate.stderr)
    result = json.loads(gate.stdout.strip().splitlines()[-1])
    assert result["issues"] == [
        "VIN: open-field segment routed at 0.300 mm but class NC_VIN requires 0.528 mm",
        "VBUS_A: open-field segment routed at 0.300 mm but class NC_VBUS requires 0.800 mm"]
