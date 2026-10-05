#!/usr/bin/env python3
"""The D5 safety screen: is a netlist design safety-relevant (mains or > 50 V,
Li-ion charging)? Such designs are still built hands-off, but the review
report opens with **HUMAN REVIEW REQUIRED** and G5 carries a warning
(review_report re-exports this API; ``safety_warnings`` gives the G5 lines).

Heuristic, rule by rule in ``detect_safety``, and biased toward flagging: a
false alarm costs a reviewer a minute, a miss can cost far more. Pure:
stdlib only, no KiCad. Split out of review_report.py to keep both modules
under the 800-line cap.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, NamedTuple, Sequence

HUMAN_REVIEW = "HUMAN REVIEW REQUIRED"
SAFETY_MAINS, SAFETY_LIION = "mains_hv", "li_ion_charging"
SAFETY_LABELS: Mapping[str, str] = {SAFETY_MAINS: "mains or > 50 V",
                                    SAFETY_LIION: "Li-ion charging"}
MAINS_VOLTAGE_LIMIT_V = 50.0       # D5: "> 50 V" (strictly greater, either polarity)
MAINS_RATING_MIN_V = 125.0         # mains fuse / MOV ratings start at 125 V (120 VAC lines)

# --- net names and part values ------------------------------------------------------
_TOKEN_SPLIT = re.compile(r"[^A-Z0-9.]+")
_NAME_VOLTS = re.compile(r"^(\d+(?:\.\d+)?)V(?:AC|DC|RMS)?$")        # 230VAC, 12V, 1.8V
_NAME_VOLTS_SPLIT = re.compile(r"^(\d+)V(\d+)$")                     # 3V3 = 3.3 V
_TEXT_VOLTS = re.compile(r"(?<![A-Z0-9.])(\d+(?:\.\d+)?)\s*V(?:AC|DC|RMS)?(?![A-Z0-9])")
_EPCOS_MOV = re.compile(r"(?<![A-Z0-9])S\d{1,2}K(\d{2,4})(?![A-Z0-9])")   # S14K275 = 275 V
_LITTELFUSE_MOV = re.compile(r"(?<![A-Z0-9])V(\d{3})LA")                 # V275LA20AP = 275 V
_DISC_MOV = re.compile(r"(?<![A-Z0-9])\d{2}D(\d{2})(\d)K")               # 14D471K = 47 x 10^1 V
MAINS_NET_TOKENS = frozenset({"MAINS", "HV", "HVDC", "LIVE", "NEUTRAL", "VAC", "ACL", "ACN",
                              "ACIN"})
AC_PAIR_TOKENS = frozenset({"L", "N", "LINE", "LIVE", "NEUTRAL", "HOT", "PHASE", "IN", "INPUT"})
# A logic level shifter's two rails (SparkFun BOB-12009 style): nets named exactly HV and
# LV, optionally with a channel number or a rail voltage of at most 50 (HV1, HV_5V, LV3V3).
# HV_400, and an isolated HV design's HV_BUS / HV+ / HV_DC_LINK beside LV_* nets, are not.
_SHIFTER_RAIL = re.compile(r"^([HL]V)(?:[_.-]?(\d+V\d+|\d+(?:\.\d+)?V?))?$")
_BATTERY_TOKEN = re.compile(r"^(V?BATT?(ERY)?\d*|V?CELL\d*|LIPO|LIION)$")
CHARGE_NET_TOKENS = frozenset({"CHG", "CHRG", "NCHG", "NCHRG", "CHARGE", "CHARGING", "CHGOK"})
_PART = r"(?<![A-Z0-9])"
CHARGER_PATTERNS: tuple[tuple[re.Pattern, str], ...] = tuple(
    (re.compile(_PART + pattern), family) for pattern, family in (
        (r"TP(40(5[4-7]|6\d)|5[01]00)", "TP405x/TP406x/TP5000/TP5100 Li-ion charger"),
        (r"MCP73([12]\d\d|8\d\d)", "Microchip MCP731xx/MCP732xx/MCP738xx Li-ion charger"),
        (r"BQ2[145]\d\d", "TI bq21xxx/bq24xxx/bq25xxx battery charger"),
        # LTC41xx only where it is a charger: LTC4150/4151 are battery monitors
        (r"(LTC(40\d\d|41(0[01]|10|2[0146]|5[56]|6[02])|355[3-6])|LT3652)",
         "ADI LTC40xx/LTC41xx/LTC355x/LT3652 battery charger"),
        (r"MAX(155[15]|181[01]|187\d|8606|890\d|1647|1648)", "Maxim Li-ion charger"),
        (r"CN3(0[5-8]\d|7\d\d)", "Consonance CN30xx/CN37xx Li-ion charger"),
        (r"IP5[1-4]\d\d", "Injoinic IP5xxx power-bank SoC (Li-ion charger)"),
        (r"AXP\d{3,4}", "X-Powers AXP PMIC with a Li-ion charger"),
        (r"(SLM6800|SGM40\d\d|LP40[56]\d|ETA60\d\d|XC680\d|STC4054|STNS01|STBC0\d|L6924"
         r"|MC3467\d|ADP506\d|LP3947|APW7261|ME4054|HX6002)", "Li-ion charger IC")))

# --- assumption text (the upper-cased value, or "<topic> <value>": see _said) --------
_MAINS_TEXT = re.compile(r"\bMAINS\b|\bLINE VOLTAGE\b|\bAC LINE\b")
_LITHIUM_TEXT = re.compile(r"\bLI[- ]?ION\b|\bLI-?PO(?:LY(?:MER)?)?\b|\bLITHIUM\b|\bLIFEPO4\b"
                           r"|\bLFP\b|(?<!\d)(?:18650|21700)(?!\d)")
_CHARGE_TEXT = re.compile(r"\b(?:RE)?CHARG[A-Z]*")                  # DISCHARGE does not match
_CLAUSE_END = re.compile(r"[;,.:!?()\[\]]")
_WORDS = re.compile(r"[A-Z0-9]+")
# Negation is read only in narrow, adjacent forms (see _negated); a miss costs more than
# a false alarm, so "not", "never", a bare "off" or "external" nearby never negate.
NEGATING_WORDS = frozenset({"NO", "WITHOUT"})     # before a term that ends its clause ...
SAME_THING_AFTER: tuple[tuple[str, ...], ...] = (  # ... or is followed only by these
    ("POWER",), ("SUPPLY",), ("INPUT",), ("CONNECTION",), ("CIRCUIT",), ("CIRCUITRY",),
    ("IC",), ("ON", "BOARD"), ("ON", "THE", "BOARD"), ("ONBOARD",))
PREFIX_NEGATION = "NON"                           # directly before the term: always
ON_BOARD_WORDS: tuple[tuple[str, ...], ...] = (("ON", "BOARD"), ("ONBOARD",), ("LOCAL",))
OFF_BOARD_AFTER: tuple[tuple[str, ...], ...] = (                  # directly after the term
    ("OFF", "BOARD"), ("OFF", "THE", "BOARD"), ("OFFBOARD",))
ELSEWHERE_WORDS = frozenset({"EXTERNALLY", "ELSEWHERE"})  # after it, at clause end or a link
BOARD_NOUNS = frozenset({"BOARD", "PCB", "PCBA", "DESIGN"})  # "elsewhere on the board": here
EXTERNAL_WORDS: tuple[tuple[str, ...], ...] = (("EXTERNAL",), ("OFFBOARD",), ("OFF", "BOARD"))
ADAPTER_WORDS = frozenset({"ADAPTER", "ADAPTERS", "ADAPTOR", "ADAPTORS", "BRICK", "PSU"})
CHARGER_WORDS = frozenset({"CHARGER", "CHARGERS"})
LINK_WORDS = frozenset({"BY", "WITH", "USING", "IN", "ON", "FROM", "VIA", "A", "AN", "THE"})


class SafetyFlag(NamedTuple):
    """One reason a design needs human review: ``kind`` is SAFETY_MAINS or
    SAFETY_LIION, ``reason`` names the net, part or assumption."""
    kind: str
    reason: str


def _tokens(name: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(name.upper()) if t]


def _name_volts(tokens: list[str]) -> float:
    found = [float(m.group(1)) for m in map(_NAME_VOLTS.match, tokens) if m]
    found += [float(f"{m.group(1)}.{m.group(2)}")
              for m in map(_NAME_VOLTS_SPLIT.match, tokens) if m]
    return max(found, default=0.0)


def _text_volts(text: str) -> float:
    upper = text.upper()
    found = [float(v) for pattern in (_TEXT_VOLTS, _EPCOS_MOV, _LITTELFUSE_MOV)
             for v in pattern.findall(upper)]
    found += [float(digits) * 10 ** int(exponent)
              for digits, exponent in _DISC_MOV.findall(upper)]
    return max(found, default=0.0)


def _shifter_rail(name: str) -> str | None:
    """'HV' / 'LV' when ``name`` is a level shifter's rail: exactly HV or LV,
    optionally with a channel number or rail voltage of at most 50 (HV1,
    HV_5V, LV3V3); None otherwise (HV_400, HV_BUS, HV+)."""
    m = _SHIFTER_RAIL.match(name.upper())
    if m is None:
        return None
    suffix = m.group(2) or "0"
    split = _NAME_VOLTS_SPLIT.match(suffix)
    value = float(f"{split.group(1)}.{split.group(2)}") if split else float(suffix.rstrip("V"))
    return m.group(1) if value <= MAINS_VOLTAGE_LIMIT_V else None


def _ends_with(words: Sequence[str], phrases: Sequence[tuple[str, ...]]) -> int:
    """Length of the first of ``phrases`` that ``words`` ends with, 0 for none."""
    return next((len(p) for p in phrases if tuple(words[len(words) - len(p):]) == p), 0)


def _starts_with(words: Sequence[str], phrases: Sequence[tuple[str, ...]]) -> int:
    """Length of the first of ``phrases`` that ``words`` starts with, 0 for none."""
    return next((len(p) for p in phrases if tuple(words[:len(p)]) == p), 0)


def _external_before(before: Sequence[str], gap: int) -> bool:
    """EXTERNAL / OFF-BOARD ends ``before``, or precedes at most ``gap`` of
    its last words ("external USB charger": gap 1)."""
    return any(_ends_with(before[:len(before) - i], EXTERNAL_WORDS) for i in range(gap + 1))


def _external_charger_object(after: Sequence[str]) -> bool:
    """'charged by an external (USB) charger': past a preposition and an
    article, the mention's object is an off-board charger. An external
    *adapter* or part feeding an on-board charger is not."""
    rest = list(after)
    for _ in range(2):
        rest = rest[1:] if rest[:1] and rest[0] in LINK_WORDS else rest
    n = _starts_with(rest, EXTERNAL_WORDS)
    return n > 0 and bool(CHARGER_WORDS.intersection(rest[n:n + 3]))


def _only_same_thing(after: Sequence[str]) -> bool:
    """``after`` is empty or made only of SAME_THING_AFTER words ("no mains
    power supply", "no charging on board"), not of another noun ("no mains
    input fuse": NO is about the fuse)."""
    rest, n = list(after), _starts_with(after, SAME_THING_AFTER)
    while n:
        rest = rest[n:]
        n = _starts_with(rest, SAME_THING_AFTER)
    return not rest


def _placed_off_board(after: Sequence[str]) -> bool:
    """OFF-BOARD / OFF THE BOARD directly after the term, or EXTERNALLY /
    ELSEWHERE at clause end or before a link word ("charged externally by
    the user"), but not before an on-board place ("elsewhere on the board")
    or another word ("externally fused", "externally supplied")."""
    if _starts_with(after, OFF_BOARD_AFTER):
        return True
    if not after or after[0] not in ELSEWHERE_WORDS:
        return False
    rest = after[1:]
    return not rest or (rest[0] in LINK_WORDS and not BOARD_NOUNS.intersection(rest[1:3]))


def _negated(term: str, before: Sequence[str], after: Sequence[str]) -> bool:
    """Whether one mention of ``term`` (the words of its clause ``before`` and
    ``after`` it) is negated or placed off the board; the forms are listed
    in detect_safety."""
    if before and before[-1] == PREFIX_NEGATION:         # "non-rechargeable"
        return True
    head, skip = list(before), _ends_with(before, ON_BOARD_WORDS)
    while skip:                                          # "no on-board charging"
        head = head[:-skip]
        skip = _ends_with(head, ON_BOARD_WORDS)
    if head[-1:] and head[-1] in NEGATING_WORDS and _only_same_thing(after):
        return True
    if _placed_off_board(after):
        return True
    if _WORDS.findall(term)[-1] in CHARGER_WORDS:           # "external (USB) charger"
        return _external_before(before, 2) or _external_charger_object(after)
    rest = after[1:] if after[:1] == ["POWER"] else after
    if _external_before(before, 0) and ADAPTER_WORDS.intersection(rest[:1]):
        return True                                      # "external mains (power) adapter"
    return _external_charger_object(after)


def _mentioned(text: str, pattern: re.Pattern) -> bool:
    """``pattern`` occurs in the upper-cased ``text`` at least once that is
    neither negated nor placed off the board within its clause (_negated)."""
    for m in pattern.finditer(text):
        before = _WORDS.findall(_CLAUSE_END.split(text[:m.start()])[-1])
        after = _WORDS.findall(_CLAUSE_END.split(text[m.end():])[0])
        if not _negated(m.group(0), before, after):
            return True
    return False


def _mains_net_reason(name: str, level_shifter: bool) -> str | None:
    """Why net ``name`` looks like mains / > 50 V, or None. With
    ``level_shifter`` (an LV rail net exists) a net named like a shifter's HV
    rail is let off its HV token; HV_BUS, HV+ and friends never are."""
    tokens = _tokens(name)
    shifter_rail = level_shifter and _shifter_rail(name) == "HV"
    named = MAINS_NET_TOKENS.intersection(tokens) - ({"HV"} if shifter_rail else set())
    if named or ("AC" in tokens and AC_PAIR_TOKENS.intersection(tokens)):
        return f"net '{name}' is named like a mains / high-voltage net"
    volts = _name_volts(tokens)
    if volts > MAINS_VOLTAGE_LIMIT_V:
        return f"net '{name}' names {volts:g} V"
    return None


def _dicts(items: Any) -> list[Mapping]:
    return [i for i in items if isinstance(i, Mapping)] if isinstance(items, list) else []


def _net_names(design: Mapping) -> list[str]:
    blocks = (design.get("nets"), design.get("net_specs"))
    return sorted({str(n) for block in blocks if isinstance(block, Mapping) for n in block})


def _part_mains(comp: Mapping) -> str | None:
    ref = str(comp.get("ref", "?"))
    lib, _, sym = str(comp.get("lib_id") or "").partition(":")
    fp_lib = str(comp.get("footprint") or "").partition(":")[0]
    value = str(comp.get("value") or "")
    rated = _text_volts(f"{value} {comp.get('mpn') or ''}")
    if "Converter_ACDC" in (lib, fp_lib):
        return f"{ref} ({value or sym}) is an AC-DC converter module (mains input)"
    if lib == "Connector" and sym.startswith("Conn_Plug_"):
        return f"{ref} ({sym}) is a mains plug connector"
    if ("FUSE" in sym.upper() or fp_lib == "Fuse") and rated >= MAINS_RATING_MIN_V:
        return f"fuse {ref} ({value}) is rated {rated:g} V (mains class)"
    if ("VARISTOR" in sym.upper() or fp_lib == "Varistor") and rated >= MAINS_RATING_MIN_V:
        return f"varistor {ref} ({value}) is rated {rated:g} V (mains surge protection)"
    return None


def _part_charger(comp: Mapping) -> str | None:
    sym = str(comp.get("lib_id") or "").partition(":")[2]
    value = str(comp.get("value") or "")
    texts = [t.upper() for t in (sym, value, str(comp.get("mpn") or "")) if t]
    for pattern, family in CHARGER_PATTERNS:
        if any(pattern.search(t) for t in texts):
            return f"{comp.get('ref', '?')} ({value or sym}): {family}"
    return None


class _Said(NamedTuple):
    """What one assumption states: its report label, a short form for a
    cross-assumption reason, and whether it states each safety term."""
    label: str
    brief: str
    mains: bool
    lithium: bool
    charge: bool


def _said(topic: str, value: str, pattern: re.Pattern) -> bool:
    """Whether an assumption states ``pattern``: the value decides when it
    names the term itself (topic 'charging', value 'no on-board charging');
    otherwise topic and value read as one clause ('charging' + '500 mA from
    USB-C' states charging, 'charging' + 'off-board' does not)."""
    upper = value.upper()
    if pattern.search(upper):
        return _mentioned(upper, pattern)
    return _mentioned(f"{topic} {value}".upper(), pattern)


def _read_assumption(entry: Mapping) -> _Said:
    value = entry.get("value")
    shown = value if isinstance(value, str) else json.dumps(value)
    topic = str(entry.get("topic", ""))
    ident, shown_topic = entry.get("id", "?"), entry.get("topic", "?")
    mains = (_said(topic, shown, _MAINS_TEXT)
             or _text_volts(f"{topic} {shown}") > MAINS_VOLTAGE_LIMIT_V)
    return _Said(f"assumption {ident} ({shown_topic}): {shown}",
                 f"{ident} ({shown_topic}: {shown})", mains,
                 _said(topic, shown, _LITHIUM_TEXT), _said(topic, shown, _CHARGE_TEXT))


def _assumption_flags(design: Mapping) -> list[SafetyFlag]:
    """Per assumption: mains / > 50 V, and lithium with charging. D5 records
    assumptions per topic, so lithium in one and charging in another (cell
    'battery: 1S LiPo', 'charging: 500 mA from USB-C') is one more flag
    naming them all, unless one assumption already states both."""
    said = [_read_assumption(e) for e in _dicts(design.get("assumptions"))]
    flags = [SafetyFlag(SAFETY_MAINS, s.label) for s in said if s.mains]
    both = [SafetyFlag(SAFETY_LIION, s.label) for s in said if s.lithium and s.charge]
    lithium = [s.brief for s in said if s.lithium]
    charge = [s.brief for s in said if s.charge]
    if both or not (lithium and charge):
        return flags + both
    return flags + [SafetyFlag(SAFETY_LIION, f"lithium in assumption(s) {', '.join(lithium)} "
                                             f"+ charging in {', '.join(charge)}")]


def _net_flags(design: Mapping, names: list[str]) -> list[SafetyFlag]:
    flags: list[SafetyFlag] = []
    specs = design.get("net_specs") if isinstance(design.get("net_specs"), Mapping) else {}
    for net, spec in sorted(specs.items()):
        volts = spec.get("voltage") if isinstance(spec, Mapping) else None
        if (isinstance(volts, (int, float)) and not isinstance(volts, bool)
                and abs(volts) > MAINS_VOLTAGE_LIMIT_V):
            flags.append(SafetyFlag(SAFETY_MAINS,
                                    f"net '{net}' is specified at {volts:g} V (net_specs)"))
    shifter = any(_shifter_rail(n) == "LV" for n in names)
    flags += [SafetyFlag(SAFETY_MAINS, r) for r in (_mains_net_reason(n, shifter) for n in names)
              if r]
    if {"L", "N"} <= {n.upper() for n in names}:
        flags.append(SafetyFlag(SAFETY_MAINS, "nets 'L' and 'N' look like mains line and neutral"))
    return flags


def _charger_flags(comps: list[Mapping], names: list[str]) -> list[SafetyFlag]:
    battery = [n for n in names if any(_BATTERY_TOKEN.match(t) for t in _tokens(n))]
    chargers = [r for r in map(_part_charger, comps) if r]
    suffix = f"; battery net(s) {', '.join(battery)}" if battery else ""
    flags = [SafetyFlag(SAFETY_LIION, r + suffix) for r in chargers]
    charge_nets = [n for n in names if CHARGE_NET_TOKENS.intersection(_tokens(n))]
    if not chargers and battery and charge_nets:
        flags.append(SafetyFlag(SAFETY_LIION, f"battery net(s) {', '.join(battery)} with "
                                              f"charge-control net(s) {', '.join(charge_nets)} "
                                              f"(no charger IC recognised)"))
    return flags


def detect_safety(design: Mapping) -> list[SafetyFlag]:
    """Heuristic D5 safety screen; [] for an ordinary low-voltage design.

    Mains or > 50 V (SAFETY_MAINS):
      * a ``net_specs`` voltage of magnitude > 50 V (exactly 50 V is not);
      * a net name with a token MAINS, HV, HVDC, LIVE, NEUTRAL, VAC, ACL, ACN
        or ACIN, or AC with L / N / LINE / LIVE / NEUTRAL / HOT / PHASE / IN /
        INPUT (AC_L, L_AC, ACIN_N, AC_IN), or a voltage token > 50 V (230VAC,
        +170V); nets named exactly L and N. One exemption, a logic level
        shifter's rails: a net named HV, optionally with a channel or rail
        suffix of at most 50 (HV1, HV_5V), is not flagged for its HV token
        when a net named LV (LV1, LV_3V3) exists. HV_400, HV_BUS, HV+, HV-,
        HV_DC_LINK and HVDC count beside any LV_* net;
      * a Converter_ACDC symbol or footprint (AC-DC module), a
        Connector:Conn_Plug_* mains plug;
      * a fuse or varistor whose value / MPN states a mains-class rating,
        >= 125 V (125 V / 250 V fuses, 130 / 275 VAC MOVs, EPCOS S14K275,
        Littelfuse V275LA20AP, disc code 14D471K = 47 x 10^1 = 470 V).
        60 V / 63 V low-voltage fuses, polyfuses and a 14D820K are not.
    Li-ion charging (SAFETY_LIION):
      * a part whose symbol name, value or MPN matches CHARGER_PATTERNS
        (TP4056, TP5100, MCP73831, MCP73213, bq24xxx/bq25xxx, LTC4054,
        LTC4162, IP5306, AXP2101, ...), the battery nets (VBAT, BAT, BATT,
        VCELL, LIPO) named in the reason;
      * without a recognised charger, a battery net together with a
        charge-control net (CHG, CHRG, CHARGE, ...).
    Either, from the netlist ``assumptions``: mains / line voltage or a
    voltage > 50 V; Li-ion (Li ion) / LiPo / Li-Poly(mer) / lithium /
    LiFePO4 / LFP / 18650 / 21700 with (re)charging (DISCHARGE is not
    charging), in one assumption or across several: D5 records them per
    topic, so 'battery: 1S LiPo' + 'charging: 500 mA from USB-C' is one flag
    naming both ids. A value decides when it names the term itself;
    otherwise topic and value read as one clause ('charging: off-board').
    A mention does not count only in these narrow forms, within its clause
    (_negated):
      * NON directly before it ("non-rechargeable");
      * NO or WITHOUT directly before it (an on-board / local between
        allowed) when it ends its clause or only SAME_THING_AFTER words
        follow ("no mains", "no mains power supply", "no on-board
        charging", "without charging circuitry", "no charger IC");
      * OFF-BOARD / OFF THE BOARD directly after it, or EXTERNALLY /
        ELSEWHERE directly after it at clause end or before a link word
        ("charged off-board", "charged externally by the user");
      * it names an off-board unit: EXTERNAL / OFF-BOARD directly before a
        charger or a mains (power) adapter ("external charger", "external
        mains adapter"), or a charge verb whose object is one ("charged by
        an external USB charger").
    Not negation, on purpose: NOT / NEVER (a charging constraint: "do not
    charge below 0 C"); NO / WITHOUT before a term that another noun
    follows ("no mains input fuse", "no mains isolation", "no charge LED":
    the noun is negated); EXTERNALLY / ELSEWHERE before another word or an
    on-board place ("mains externally fused", "charging externally
    supplied", "charging elsewhere on the board"); a bare OFF or EXTERNAL
    elsewhere ("off-line mains", "on/off switch", "external IEC inlet"); an
    external *adapter* or part feeding an on-board charger ("charging from
    an external 5 V adapter", "with external NTC").

    Not flagged on purpose: voltage ratings of parts other than fuses and
    varistors (a 100 V cap on a 12 V rail), a VBAT net alone (RTC backup
    cell), fuel gauges, coulomb counters (LTC4150) and protection ICs alone.
    Known false positives: a 250 V glass fuse on a low-voltage input; an
    HV_EN boost-enable net in a design without an LV net; an ACIN / AC_IN
    net that is a PMIC's 5 V adapter input or a 24 VAC input; negation that
    is not adjacent or meets a qualifier ("charging is not done here",
    "charging handled off-board", "no mains present", "no Li-ion
    charging", "12 V from a mains adapter" without "external"); a topic its
    value does not repeat ("mains: none", "charging: none" beside a LiPo).
    Known misses: a mains design with no voltage spec, no mains-named net,
    no AC-DC module and no mains-rated fuse or varistor (MOV codes other
    than S14K275, V275LA and 14D471K forms are not decoded); a charger IC
    that CHARGER_PATTERNS does not list, on a board with no CHG/CHRG-style
    net; a cell named only by another size code (14500, 26650); an isolated
    HV design whose two sides are nets named exactly HV and LV, bare or
    with a suffix of at most 50 (the level-shifter pattern)."""
    names = _net_names(design)
    comps = _dicts(design.get("components"))
    flags = (_net_flags(design, names) + [SafetyFlag(SAFETY_MAINS, r)
                                          for r in map(_part_mains, comps) if r]
             + _charger_flags(comps, names) + _assumption_flags(design))
    unique = list(dict.fromkeys(flags))
    return ([f for f in unique if f.kind == SAFETY_MAINS]
            + [f for f in unique if f.kind == SAFETY_LIION])


def safety_lines(flags: Sequence[SafetyFlag]) -> list[str]:
    """One ``<kind label>: <reason>; <reason>`` line per kind present."""
    return [f"{label}: {'; '.join(f.reason for f in flags if f.kind == kind)}"
            for kind, label in SAFETY_LABELS.items() if any(f.kind == kind for f in flags)]


def safety_warnings(design: Mapping) -> list[str]:
    """The G5 warning lines D5 asks for: one ``HUMAN REVIEW REQUIRED: <kind>:
    <reasons>`` line per safety kind detected; [] for an ordinary design."""
    return [f"{HUMAN_REVIEW}: {line}" for line in safety_lines(detect_safety(design))]
