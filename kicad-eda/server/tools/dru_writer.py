"""The managed custom-rules file <board stem>.kicad_dru (ADR D7.8; 2a families R6, R7, R8).

KiCad (SWIG LoadBoard, the zone filler and kicad-cli DRC, 9.0 to 11) reads the
rules file beside the BOARD (critique M13), so the file is written there, never at
the netlist stem. It is also the only wave-2 guard of the KiCad 11 agent path:
every route_check DRC runs these rules.

    base_rules(stackup, lines, *, pairs) -> [Rule]   the 2a families:
        R8  per plane-role layer L of net N:
            (layer "L") (condition "A.Type == 'Track' && A.NetName != 'N'")
            (constraint disallow track)
        R7  per impedance net and every routable (signal / mixed) layer NOT allowed
            for it: (layer "L") (condition "A.NetName == 'net'") (constraint disallow track)
        R6  per impedance net and allowed layer: (layer "L") (condition
            "A.NetName == 'net'") (constraint track_width (min lo) (opt w) (max hi))
            from the line's width and width_window (solved +/- max(0.005 mm, 1.5 %)).
        A diff line's rules also go to its partner net (`pairs`, pairs_of(design), or
        the record's own `diff_pair`) unless the partner has a record of its own.
        `stackup` is a stackup_model.ResolvedStackup or its artifact dict (to_dict());
        `lines` is {net: record} as impedance.resolve_impedance or the artifact's
        `lines` section gives it (`layers` {layer: {width, width_window}}; a record
        without layers, e.g. status 'error', gets no rule). Order: R8, R7, R6 (most
        specific last), nets sorted, layers top down: same inputs, same bytes.
    render(rules) -> '(version 1)' + render_block(rules)
    render_block(rules) -> the block between BEGIN_MARKER / END_MARKER, the canary
        (drc_guard.CANARY_RULE, verbatim, so rules_compile adds nothing) first.
        Raises ValueError, before anything is written, for a rule KiCad would drop
        or misread: a function call not A./B.-qualified (critique E1: a bare
        intersectsArea crashes 9.0.4, is dropped by 10.0.5, rc 3 on 10.99), a
        'disallow zone' (10.99 flags zone outlines), a 'deg' unit, a layer clause on
        an assertion (ignored by KiCad), a layer outside F.Cu / In1..In30.Cu / B.Cu /
        outer / inner, a double quote, backslash or newline in a name or condition,
        unbalanced quotes or parentheses, or a duplicate name.
    merge(existing, block) -> the file text: the managed block replaced in place, or
        appended after the user's rules (so managed rules win where KiCad takes the
        last matching rule); text outside the markers stays byte-identical (CRLF
        included); '(version 1)' is prepended only when the file has none.
    dru_path_for(board_path) -> <board stem>.kicad_dru (a .kicad_pcb path only)
    write(board_path, rules) -> {path, sha256, rules}: merge into the existing file
        (UTF-8 only) and replace it atomically; unchanged text is not rewritten.
        sha256 is of the whole file, rules counts the managed rules (canary included).

Net names go into conditions as single-quoted literals and rule names, so a name with
a quote or backslash (ST-E11: they cannot be escaped), a double quote, a control
character or a KiCad wildcard (* or ?: 'A.NetName == ...' would match other nets) is
refused with ValueError. KiCad compares these strings without regard to case, so two
nets differing only in case share their rules.

Stdlib only (plus drc_guard) and Python 3.11-compatible.
"""
from __future__ import annotations

import contextlib
import hashlib
import math
import os
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import drc_guard

VERSION_HEADER = drc_guard.DRU_HEADER
BEGIN_MARKER = "# kicad-eda managed begin"
END_MARKER = "# kicad-eda managed end"
BLOCK_NOTE = (
    "# Written by kicad-eda from the stackup and the impedance lines (ADR D7.8). Everything",
    "# between these markers is rewritten on every build: keep your own rules outside them",
    "# (for one constraint KiCad uses the last matching rule, so a rule below the end",
    "# marker overrides a managed one).",
    "# canary: fires only on net __KICAD_EDA_CANARY__ and proves this file compiled")
DRU_SUFFIX = drc_guard.DRU_SUFFIX
BOARD_SUFFIX = ".kicad_pcb"
CANARY_NAME = "__kicad_eda_canary"
PLANE_PREFIX = "kicad_eda_plane_"
OFFLAYER_PREFIX = "kicad_eda_offlayer_"
WIDTH_PREFIX = "kicad_eda_width_"
FAMILIES = {"R8": PLANE_PREFIX, "R7": OFFLAYER_PREFIX, "R6": WIDTH_PREFIX}
FAMILY_TITLES = {
    PLANE_PREFIX: "# R8 plane-role layers: tracks of any other net are not allowed",
    OFFLAYER_PREFIX: "# R7 impedance nets: tracks only on their solved layers",
    WIDTH_PREFIX: "# R6 impedance nets: track width inside the solved window (min / opt / max)"}
PLANE_ROLE = "plane"
ROUTABLE_ROLES = ("signal", "mixed")
DIFF = "diff"
MM_DECIMALS = 6                                   # 1 nm, KiCad's internal unit

_UNSAFE_NET = re.compile(r"['\"\\*?\x00-\x1f\x7f]")
_UNSAFE_TEXT = re.compile(r"[\"\\\x00-\x1f\x7f]")
_COPPER = re.compile(r"(?:F|B)\.Cu|In(?:[1-9]|[12][0-9]|30)\.Cu|outer|inner")
_SQ_STRING = re.compile(r"'[^']*'")
_DQ_STRING = re.compile(r'"([^"]*)"')
_CALL = re.compile(r"((?:[A-Za-z_]\w*\s*\.\s*)*)([A-Za-z_]\w*)\s*\(")
_DEG = re.compile(r"\d\s*deg", re.I)
_VERSION = re.compile(r"^\ufeff?[ \t]*\(\s*version\s+\d+\s*\)", re.M)
_QUALIFIERS = ("A.", "B.")


@dataclass(frozen=True)
class Rule:
    """One custom rule: `constraint` is the text inside (constraint ...), e.g.
    'disallow track' or 'track_width (min 0.13mm) (opt 0.135mm) (max 0.14mm)'."""

    name: str
    layer: str | None
    condition: str | None
    constraint: str

    def render(self) -> str:
        parts = [f'(rule "{self.name}"']
        if self.layer is not None:
            parts.append(f'  (layer "{self.layer}")')
        if self.condition is not None:
            parts.append(f'  (condition "{self.condition}")')
        parts.append(f"  (constraint {self.constraint}))")
        return "\n".join(parts)


@dataclass(frozen=True)
class _Copper:
    name: str
    role: str
    net: str | None


@dataclass(frozen=True)
class _Window:
    layer: str
    lo: float
    opt: float
    hi: float


# ---------------------------------------------------------------- small helpers
def mm(value: float) -> str:
    """A length in KiCad rule syntax: '0.3546mm' (1 nm resolution, no exponent)."""
    text = f"{float(value):.{MM_DECIMALS}f}".rstrip("0").rstrip(".")
    return f"{text}mm"


def net_literal(net: Any) -> str:
    """The quoted literal of a net name for a condition; ValueError when KiCad cannot
    match it exactly (module docstring)."""
    if not isinstance(net, str) or not net or _UNSAFE_NET.search(net):
        raise ValueError(
            f"net name {net!r} cannot be written into a .kicad_dru condition: a quote or "
            "backslash cannot be escaped there (ST-E11), a double quote or control character "
            "breaks the file and KiCad treats * and ? as wildcards; rename the net")
    return f"'{net}'"


def family_of(rule: Rule) -> str | None:
    """'R8' | 'R7' | 'R6' for a base rule, else None."""
    return next((f for f, prefix in FAMILIES.items() if rule.name.startswith(prefix)), None)


def _field(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, Mapping) else getattr(obj, key, None)


def _positive(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def _is_diff(record: Mapping[str, Any]) -> bool:
    return DIFF in (record.get("kind"), record.get("mode"))


# ---------------------------------------------------------------- inputs
def _copper(stackup: Any) -> tuple[_Copper, ...]:
    """The copper layers (name, role, net) of a ResolvedStackup or its to_dict()."""
    entries = _field(stackup, "copper")
    if not isinstance(entries, (list, tuple)) or not entries:
        raise ValueError("stackup has no copper layers: pass a stackup_model.ResolvedStackup "
                         "or its artifact dict (to_dict())")
    out = []
    for entry in entries:
        name, role, net = (_field(entry, k) for k in ("name", "role", "net"))
        if not isinstance(name, str) or not isinstance(role, str):
            raise ValueError(f"stackup copper entry {entry!r} needs a name and a role")
        out.append(_Copper(name, role, net if isinstance(net, str) else None))
    return tuple(out)


def _window(net: str, layer: str, line: Any) -> _Window:
    where = f"lines.{net}.layers.{layer}"
    if not isinstance(line, Mapping):
        raise ValueError(f"{where} must be a line record, got {line!r}")
    width, window = line.get("width"), line.get("width_window")
    if not _positive(width):
        raise ValueError(f"{where}.width must be a number > 0 mm, got {width!r}")
    if not isinstance(window, (list, tuple)) or len(window) != 2:
        raise ValueError(f"{where}.width_window must be [min, max] in mm, got {window!r}")
    lo, hi = window
    if not (_positive(lo) and _positive(hi) and lo <= hi):
        raise ValueError(f"{where}.width_window {list(window)!r} is not a window "
                         "0 < min <= max (mm)")
    if not lo <= width <= hi:
        raise ValueError(f"{where}.width {width!r} lies outside its width_window "
                         f"{list(window)!r}")
    return _Window(layer, float(lo), float(width), float(hi))


def _windows(net: str, record: Mapping[str, Any],
             names: Sequence[str]) -> tuple[_Window, ...] | None:
    """The record's per-layer windows top down, or None for a line without layers."""
    layers = record.get("layers")
    if layers is None or (isinstance(layers, Mapping) and not layers):
        return None
    if not isinstance(layers, Mapping):
        raise ValueError(f"lines.{net}.layers must be a {{layer: line record}} map, "
                         f"got {layers!r}")
    unknown = [la for la in layers if la not in names]
    if unknown:
        raise ValueError(f"lines.{net}.layers names {unknown!r}, not a copper layer of the "
                         f"stackup {list(names)!r}")
    return tuple(_window(net, la, layers[la]) for la in names if la in layers)


def _targets(lines: Mapping[str, Any], pairs: Mapping[str, str],
             names: Sequence[str]) -> list[tuple[str, tuple[_Window, ...]]]:
    """[(net, windows)] sorted by net: every resolved line, plus the partner of each
    diff line that has no record of its own."""
    if not isinstance(lines, Mapping):
        raise ValueError(f"lines must be a {{net: line record}} map, got {lines!r}")
    own: dict[str, tuple[tuple[_Window, ...], Mapping[str, Any]]] = {}
    for net, record in lines.items():
        if not isinstance(record, Mapping):
            raise ValueError(f"lines.{net} must be a line record, got {record!r}")
        windows = _windows(net, record, names)
        if windows is not None:
            own[net] = (windows, record)
    out = {net: windows for net, (windows, _) in own.items()}
    for net in sorted(own):
        windows, record = own[net]
        partner = record.get("diff_pair") or pairs.get(net)
        if _is_diff(record) and isinstance(partner, str) and partner != net \
                and partner not in lines:
            out.setdefault(partner, windows)
    for net in out:
        net_literal(net)
    return sorted(out.items())


def pairs_of(design: Mapping[str, Any]) -> dict[str, str]:
    """{net: partner} both ways from net_specs[*].diff_pair (first declaration wins)."""
    specs = design.get("net_specs") if isinstance(design, Mapping) else None
    out: dict[str, str] = {}
    if not isinstance(specs, Mapping):
        return out
    for net in sorted(specs):
        spec = specs[net]
        partner = spec.get("diff_pair") if isinstance(spec, Mapping) else None
        if isinstance(partner, str) and partner and partner != net:
            out.setdefault(net, partner)
            out.setdefault(partner, net)
    return out


# ---------------------------------------------------------------- families
def _plane_rules(copper: Sequence[_Copper]) -> list[Rule]:
    """R8: no foreign-net tracks on a plane-role layer."""
    out = []
    for c in copper:
        if c.role != PLANE_ROLE:
            continue
        if not c.net:
            raise ValueError(f"plane-role layer {c.name} has no net (resolve the stackup first)")
        out.append(Rule(PLANE_PREFIX + c.name, c.name,
                        f"A.Type == 'Track' && A.NetName != {net_literal(c.net)}",
                        "disallow track"))
    return out


def _offlayer_rules(targets: Sequence[tuple[str, tuple[_Window, ...]]],
                    routable: Sequence[str]) -> list[Rule]:
    """R7: an impedance net's tracks only on its solved layers."""
    return [Rule(f"{OFFLAYER_PREFIX}{net}_{layer}", layer, f"A.NetName == {net_literal(net)}",
                 "disallow track")
            for net, windows in targets
            for layer in routable if layer not in {w.layer for w in windows}]


def _width_rules(targets: Sequence[tuple[str, tuple[_Window, ...]]]) -> list[Rule]:
    """R6: the solved width window per net and allowed layer."""
    return [Rule(f"{WIDTH_PREFIX}{net}_{w.layer}", w.layer, f"A.NetName == {net_literal(net)}",
                 f"track_width (min {mm(w.lo)}) (opt {mm(w.opt)}) (max {mm(w.hi)})")
            for net, windows in targets for w in windows]


def base_rules(stackup: Any, lines: Mapping[str, Any] | None, *,
               pairs: Mapping[str, str] | None = None) -> list[Rule]:
    """The 2a families R8, R7, R6 (module docstring). Never mutates its inputs."""
    copper = _copper(stackup)
    names = tuple(c.name for c in copper)
    targets = _targets(lines or {}, dict(pairs or {}), names)
    routable = tuple(c.name for c in copper if c.role in ROUTABLE_ROLES)
    return [*_plane_rules(copper), *_offlayer_rules(targets, routable), *_width_rules(targets)]


# ---------------------------------------------------------------- render guards
def _balanced(text: str) -> bool:
    depth = 0
    for ch in text:
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if depth < 0:
            return False
    return depth == 0


def _check_calls(rule: Rule, expr: str) -> None:
    """Every function call in `expr` is A.- or B.-qualified (critique E1)."""
    if expr.count("'") % 2:
        raise ValueError(f"rule {rule.name!r}: unbalanced single quote in {expr!r}")
    code = _SQ_STRING.sub("''", expr)
    if not _balanced(code):
        raise ValueError(f"rule {rule.name!r}: unbalanced parentheses in {expr!r}")
    for match in _CALL.finditer(code):
        prefix = re.sub(r"\s+", "", match.group(1))
        if prefix not in _QUALIFIERS:
            raise ValueError(
                f"rule {rule.name!r}: function call {prefix}{match.group(2)}() in {expr!r} is "
                "not A./B.-qualified (a bare call crashes KiCad 9.0.4 DRC, is dropped by 10.0.5 "
                "and fails on 10.99); write A.<function>(...) or B.<function>(...)")


def _check_name_and_layer(rule: Rule) -> None:
    if not isinstance(rule.name, str) or not rule.name or _UNSAFE_TEXT.search(rule.name):
        raise ValueError(f"rule name {rule.name!r} must be non-empty text without a double "
                         "quote, backslash or control character")
    if rule.layer is not None and not (isinstance(rule.layer, str)
                                       and _COPPER.fullmatch(rule.layer)):
        raise ValueError(f"rule {rule.name!r}: layer {rule.layer!r} is not one of F.Cu, "
                         "In1.Cu..In30.Cu, B.Cu, outer, inner (an unknown layer makes KiCad "
                         "drop the whole rules file)")


def _check_condition(rule: Rule) -> None:
    cond = rule.condition
    if cond is None:
        return
    if not isinstance(cond, str) or not cond.strip() or _UNSAFE_TEXT.search(cond):
        raise ValueError(f"rule {rule.name!r}: condition {cond!r} must be non-empty text "
                         "without a double quote, backslash or newline")
    _check_calls(rule, cond)


def _check_constraint(rule: Rule) -> None:
    con = rule.constraint
    if not isinstance(con, str) or not con.strip() or re.search(r"[\\\x00-\x1f\x7f]", con):
        raise ValueError(f"rule {rule.name!r}: constraint {con!r} must be one line of text "
                         "without a backslash")
    if con.count('"') % 2:
        raise ValueError(f"rule {rule.name!r}: unbalanced double quote in constraint {con!r}")
    for expr in _DQ_STRING.findall(con):
        _check_calls(rule, expr)
    outside = _DQ_STRING.sub('""', con)
    tokens = outside.split()
    if not _balanced(outside):
        raise ValueError(f"rule {rule.name!r}: unbalanced parentheses in constraint {con!r}")
    if _DEG.search(outside):
        raise ValueError(f"rule {rule.name!r}: constraint {con!r} has a 'deg' unit; KiCad "
                         "drops the file (write track_angle (min 135))")
    if tokens[0] == "disallow" and "zone" in tokens[1:]:
        raise ValueError(f"rule {rule.name!r}: 'disallow zone' is never written (KiCad 10.99 "
                         "flags the zone outlines themselves)")
    if tokens[0] == "assertion" and rule.layer is not None:
        raise ValueError(f"rule {rule.name!r}: a (layer ...) clause does not restrict an "
                         "assertion; put the layer in the condition (A.Layer == '...')")


def check_rule(rule: Rule) -> None:
    """ValueError when KiCad would drop or misread `rule` (module docstring)."""
    if not isinstance(rule, Rule):
        raise ValueError(f"not a dru_writer.Rule: {rule!r}")
    _check_name_and_layer(rule)
    _check_condition(rule)
    _check_constraint(rule)


def _check_all(rules: Sequence[Rule]) -> None:
    seen = {CANARY_NAME}
    for rule in rules:
        check_rule(rule)
        if rule.name in seen:
            raise ValueError(f"duplicate rule name {rule.name!r} in the managed block")
        seen.add(rule.name)


# ---------------------------------------------------------------- render
def render_block(rules: Iterable[Rule]) -> str:
    """The managed block: markers, a note, the canary, then `rules` (checked first)."""
    rules = tuple(rules)
    _check_all(rules)
    body = [BEGIN_MARKER, *BLOCK_NOTE, drc_guard.CANARY_RULE]
    title = None
    for rule in rules:
        family = next((p for p in FAMILY_TITLES if rule.name.startswith(p)), None)
        if family is not None and family != title:
            body.append(FAMILY_TITLES[family])
        title = family
        body.append(rule.render())
    body.append(END_MARKER)
    return "\n".join(body) + "\n"


def render(rules: Iterable[Rule]) -> str:
    """A complete rules file: the version header and the managed block."""
    return f"{VERSION_HEADER}\n{render_block(rules)}"


# ---------------------------------------------------------------- merge
def _is_marker(line: str, marker: str) -> bool:
    text = line.strip()
    return text == marker or text.startswith(marker + " ")


def _marker_lines(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """([(start, stop)] of begin-marker lines, the same for end markers)."""
    begins, ends, pos = [], [], 0
    for line in text.splitlines(keepends=True):
        span = (pos, pos + len(line))
        if _is_marker(line, BEGIN_MARKER):
            begins.append(span)
        elif _is_marker(line, END_MARKER):
            ends.append(span)
        pos = span[1]
    return begins, ends


def _managed_span(text: str) -> tuple[int, int] | None:
    begins, ends = _marker_lines(text)
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or ends[0][0] < begins[0][1]:
        raise ValueError(
            f"the rules file has broken kicad-eda markers ({len(begins)} begin / {len(ends)} "
            f"end marker lines, or the end before the begin): fix it by hand so it holds at "
            f"most one '{BEGIN_MARKER}' ... '{END_MARKER}' block; nothing was written")
    return begins[0][0], ends[0][1]


def _check_block(block: str) -> None:
    begins, ends = _marker_lines(block)
    if not (len(begins) == 1 and len(ends) == 1 and begins[0][0] == 0
            and ends[0][1] == len(block) and block.endswith("\n")):
        raise ValueError(f"not one managed block: it must start with {BEGIN_MARKER!r} and end "
                         f"with an {END_MARKER!r} line (use render_block)")


def _with_header(text: str) -> str:
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    return f"{bom}{VERSION_HEADER}\n{text[len(bom):]}"


def merge(existing: str | None, block: str) -> str:
    """`existing` with its managed block replaced by `block` (module docstring)."""
    _check_block(block)
    if existing is None or not existing.strip():
        return f"{VERSION_HEADER}\n{block}"
    span = _managed_span(existing)
    if span is None:
        head = existing if existing.endswith(("\n", "\r")) else existing + "\n"
        tail = ""
    else:
        head, tail = existing[:span[0]], existing[span[1]:]
    merged = head + block + tail
    return merged if _VERSION.search(head + tail) else _with_header(merged)


# ---------------------------------------------------------------- files
def dru_path_for(board_path: str | os.PathLike[str]) -> Path:
    """<board stem>.kicad_dru beside the board (where KiCad reads it)."""
    board = Path(board_path)
    if board.suffix != BOARD_SUFFIX:
        raise ValueError(f"dru_path_for needs the board file (*{BOARD_SUFFIX}), got "
                         f"{str(board)!r}: KiCad reads <board stem>{DRU_SUFFIX} beside the "
                         "board, never beside the netlist")
    return board.with_suffix(DRU_SUFFIX)


def _read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} is not UTF-8 text ({exc}); kicad-eda will not rewrite it: "
                         "convert it to UTF-8 or move it aside") from exc


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write(board_path: str | os.PathLike[str], rules: Iterable[Rule]) -> dict[str, Any]:
    """Write `rules` as the managed block of <board stem>.kicad_dru (module docstring).
    Every check runs before the file is touched; the board's directory is created when
    missing (the DRU is written before the SWIG build)."""
    path = dru_path_for(board_path)
    rules = tuple(rules)
    block = render_block(rules)
    existing = _read_text(path)
    text = merge(existing, block)
    data = text.encode("utf-8")
    if existing != text:
        _atomic_write(path, data)
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(),
            "rules": len(rules) + 1}
