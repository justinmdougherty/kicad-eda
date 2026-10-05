#!/usr/bin/env python3
"""MCU interface pin-function lint rules (PM-*), ORDER 20.

Hands-off composition lets the agent choose which MCU pins carry a GNSS UART,
an I2C bus or a PPS line; DRC cannot tell a pin without a SERCOM from one that
has it. For a component whose lib_id or value names an MCU with a pin table in
server/data/pinmux/*.json (today the SAM D21 A variants, atsamd21.json from the
datasheet's Table 7-1), every MCU pad on a net whose NAME marks an interface
role is checked against the functions of its pin:

  UART  TX/TXD, RX/RXD      TX on SERCOM PAD0 or PAD2, RX on another pad of the
                            same SERCOM. Names carry no direction, so a TX/RX
                            pair is accepted either way round and a lone line
                            on any SERCOM pad (a deliberate widening of the
                            rule: GNSS_TXD is the module's TX, on the MCU's RX)
  I2C   SDA, SCL            SDA on PAD0 and SCL on PAD1 of one SERCOM
  SPI   MOSI/COPI, MISO/CIPO, SCK/SCLK
                            one SERCOM with a legal DOPO/DIPO pad combination:
                            MOSI on the DOPO's DO pad, SCK on its SCK pad, MISO
                            (DIPO) on another pad. Every legal slave setup (MISO
                            on DO, MOSI on a pad other than the slave SS) is
                            one of these, so a slave MCU is covered too
  SPI   SS/CS/NSS/NCS/CSN   recognised but free: a chip select is any GPIO
  INT   PPS, INT, IRQ       a pin with an EXTINT line whose number no other
                            PPS/INT net on that MCU uses (PA08's NMI is not one)

  PM-W1  warning  ``<ref>.<pad> (<pin name>) on net <net> cannot be <role>:
                  candidates <pin> (<pad>), ...`` -- the candidates are capable
                  pins that carry no other net (at most MAX_CANDIDATES named;
                  interrupt candidates without a SERCOM come first)
  PM-I0  info     ``pin functions not verified for <lib_id> (<refs>)``: an MCU
                  from an MCU_* / RF_Module library with no table (or a pad
                  outside its table) has interface-named nets; one per lib_id.
                  RF_Module parts that name a fixed-function transceiver
                  (FIXED_FUNCTION_RADIOS: no user code, nothing to mux) are
                  not MCUs. lint has no info level, so PM-I0 is carried as a
                  lint warning; the review lists it with G1's warnings.

Limitations (documented, by design): matching is by net name only. A name's
tokens are its letter and digit runs ('SPI1_MOSI' -> SPI, 1, MOSI); a name
must mark exactly one role, and names with an LED / EN / ENABLE token or 'I2S'
are not interfaces. Nets of one bus share the rest of their name (SPI1_MOSI and
SPI1_SCK; GNSS_TX and GNSS_RX). Flow control (RTS/CTS), the slave / hardware SS
pad and I2C high-speed pin limits are not checked.

Stdlib only and Python 3.11-safe. check() never mutates the design, never
raises on a malformed one and returns no errors.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

from lint_context import LintContext

ORDER = 20
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "pinmux"
UNVERIFIED_LIBS = re.compile(r"^(MCU_|RF_Module)")      # MCU-ish libraries PM-I0 speaks for
# RF_Module transceivers that run no user code (HopeRF RFM69/RFM9x, Ai-Thinker Ra-0x, Decawave
# DWM1000/DWM3000, bare SX12xx/LLCC68, nRF24L01, CC1101, XBee, ThingMagic): not MCUs
FIXED_FUNCTION_RADIOS = re.compile(
    r"RFM\d|\bRa-0\d|DWM[13]000|SX12\d\d|LLCC68|NRF24L01|CC1101|XBEE|THINGMAGIC", re.IGNORECASE)

ROLE_TOKENS: Mapping[str, str] = MappingProxyType({
    "TX": "TX", "TXD": "TX", "RX": "RX", "RXD": "RX", "SDA": "SDA", "SCL": "SCL",
    "MOSI": "MOSI", "COPI": "MOSI", "MISO": "MISO", "CIPO": "MISO", "SCK": "SCK", "SCLK": "SCK",
    "SS": "SS", "CS": "SS", "NSS": "SS", "NCS": "SS", "CSN": "SS",
    "PPS": "EXTINT", "INT": "EXTINT", "IRQ": "EXTINT"})
NOT_INTERFACE_TOKENS = frozenset({"LED", "EN", "ENABLE"})
NOT_INTERFACE_TEXT = ("I2S",)
BUS_OF = MappingProxyType({"TX": "UART", "RX": "UART", "SDA": "I2C", "SCL": "I2C",
                           "MOSI": "SPI", "MISO": "SPI", "SCK": "SPI"})
ROLE_LABEL = MappingProxyType({"TX": "UART TX", "RX": "UART RX", "SDA": "I2C SDA",
                               "SCL": "I2C SCL", "MOSI": "SPI MOSI", "MISO": "SPI MISO",
                               "SCK": "SPI SCK"})
# the role that stays put when a bus can be fixed by moving either line
PRIORITY = MappingProxyType({"TX": 0, "RX": 1, "SDA": 0, "SCL": 1, "SCK": 0, "MOSI": 1,
                             "MISO": 2})
# DOPO 0..3 (SERCOM SPI CTRLA): (data-out pad, SCK pad, slave SS pad)
SPI_DOPO = ((0, 1, 2), (2, 3, 1), (3, 1, 2), (0, 3, 1))
MAX_CANDIDATES = 12                             # candidate pins named in one PM-W1

Combo = Mapping[str, int]                       # role -> SERCOM pad


def _uart(either_end: bool) -> tuple[Combo, ...]:
    return tuple({"TX": tx, "RX": rx} for tx in range(4) for rx in range(4)
                 if tx != rx and (tx in (0, 2) or (either_end and rx in (0, 2))))


def _spi() -> tuple[Combo, ...]:
    """Master combinations: MOSI on DO, MISO (DIPO) on any pad but DO and SCK. A master
    without hardware SS may use the slave-SS pad for MISO; the legal slave combinations
    (MISO on DO, MOSI on neither SCK nor SS) all reappear here as master ones."""
    return tuple({"MOSI": out, "SCK": sck, "MISO": inp} for out, sck, _ in SPI_DOPO
                 for inp in range(4) if inp not in (out, sck))


_I2C: tuple[Combo, ...] = ({"SDA": 0, "SCL": 1},)
_SPI = _spi()
# bus -> (combinations that are legal, combinations candidates are suggested from)
BUS_COMBOS = MappingProxyType({"UART": (_uart(True), _uart(False)), "I2C": (_I2C, _I2C),
                               "SPI": (_SPI, _SPI)})


@dataclass(frozen=True)
class Pin:
    """One I/O pin of a package: its pad number and functions."""
    name: str
    number: str
    extint: int | None
    sercoms: frozenset[tuple[int, int]]          # (SERCOM n, PAD k) from both mux columns


@dataclass(frozen=True)
class Package:
    """One pin table bound to one package's pad numbering."""
    table: str
    pattern: re.Pattern
    pins: Mapping[str, Pin]                     # I/O pins by pad number
    other: Mapping[str, str]                    # non-I/O pad number -> name
    sercoms: tuple[int, ...]


@dataclass(frozen=True)
class Entry:
    """One MCU pad on an interface-named net."""
    net: str
    pad: str
    role: str
    label: str                                  # pin name ('PA10', 'RESET')
    pin: Pin | None                             # None: not an I/O pin


# --- tables -------------------------------------------------------------------------
def _pad_key(number: str) -> tuple[int, int, str]:
    return (0, int(number), "") if number.isdigit() else (1, 0, number)


def _package(table: str, data: Mapping[str, Any], part: Mapping[str, Any]) -> Package:
    letter = part["package"]
    pins: dict[str, Pin] = {}
    for name, row in data["pins"].items():
        number = row["pin"].get(letter)
        if number is None:
            continue
        sercoms = frozenset(tuple(row[k]) for k in ("sercom", "sercom_alt") if row.get(k))
        extint = row.get("extint")
        pins[str(number)] = Pin(name, str(number), extint if isinstance(extint, int) else None,
                                sercoms)
    ordered = {n: pins[n] for n in sorted(pins, key=_pad_key)}
    other = {str(k): str(v) for k, v in data["packages"][letter]["other_pins"].items()}
    ids = tuple(sorted({s for p in pins.values() for s, _ in p.sercoms}))
    return Package(table, re.compile(part["match"], re.IGNORECASE),
                   MappingProxyType(ordered), MappingProxyType(other), ids)


@lru_cache(maxsize=8)
def packages(data_dir: str) -> tuple[Package, ...]:
    """Every package of every pin table (*.json) in ``data_dir``; a broken
    shipped table raises (lint reports it as LINT-INTERNAL)."""
    found: list[Package] = []
    for path in sorted(Path(data_dir).glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        found += [_package(path.stem, data, part) for part in data["parts"]]
    return tuple(found)


def package_for(comp: Mapping[str, Any], pkgs: Iterable[Package]) -> Package | None:
    """The package whose part pattern the lib_id (first) or value names."""
    pkgs = tuple(pkgs)
    for text in (str(comp.get("lib_id", "")), str(comp.get("value", ""))):
        for pkg in pkgs:
            if pkg.pattern.search(text):
                return pkg
    return None


# --- net names ----------------------------------------------------------------------
def _roles(name: str) -> tuple[frozenset[str], tuple[str, ...]]:
    upper = str(name).upper()
    tokens = tuple(re.findall(r"[A-Z]+|[0-9]+", upper))
    if any(t in upper for t in NOT_INTERFACE_TEXT) or NOT_INTERFACE_TOKENS & set(tokens):
        return frozenset(), tokens
    return frozenset(ROLE_TOKENS[t] for t in tokens if t in ROLE_TOKENS), tokens


def net_role(name: str) -> tuple[str, tuple[str, ...]] | None:
    """(role, bus key) when the net name marks exactly one interface role; the
    key is the name's other tokens (SPI1_MOSI -> ('MOSI', ('SPI', '1')))."""
    roles, tokens = _roles(name)
    if len(roles) != 1:
        return None
    (role,) = roles
    return role, tuple(t for t in tokens if ROLE_TOKENS.get(t) != role)


def _pads_by_ref(nets: object) -> dict[str, list[tuple[str, str]]]:
    """ref -> [(pad, net)] in net order; malformed connections are skipped."""
    found: dict[str, list[tuple[str, str]]] = {}
    if not isinstance(nets, Mapping):
        return found
    for net, conns in nets.items():
        for conn in conns if isinstance(conns, (list, tuple)) else ():
            if isinstance(conn, (list, tuple)) and len(conn) == 2:
                found.setdefault(str(conn[0]), []).append((str(conn[1]), str(net)))
    return found


# --- findings -----------------------------------------------------------------------
def _w1(ref: str, e: Entry, role: str, candidates: Iterable[Pin]) -> str:
    names = [f"{p.name} ({p.number})" for p in candidates if p.number != e.pad]
    more = len(names) - MAX_CANDIDATES
    listed = names[:MAX_CANDIDATES] + ([f"... (+{more} more)"] if more > 0 else [])
    return (f"PM-W1 {ref}.{e.pad} ({e.label}) on net {e.net} cannot be {role}: "
            f"candidates {', '.join(listed) or 'none'}")


def _fits(e: Entry, sercom: int, combo: Combo) -> bool:
    return e.pin is not None and (sercom, combo[e.role]) in e.pin.sercoms


def _project(combos: Iterable[Combo], roles: set[str]) -> tuple[Combo, ...]:
    """The combinations restricted to ``roles``, each once."""
    seen = dict.fromkeys(tuple(sorted((r, c[r]) for r in roles)) for c in combos)
    return tuple(dict(items) for items in seen)


def _candidates(pkg: Package, e: Entry, group: Sequence[Entry], top: Sequence[tuple],
                advice: Sequence[Combo], blocked: frozenset[str]) -> list[Pin]:
    """Pins that can take ``e``'s role while the group's fitting lines stay put."""
    wanted: set[tuple[int, int]] = set()
    for sercom, combo in top:
        kept = [m for m in group if _fits(m, sercom, combo)]
        wanted |= {(sercom, c[e.role]) for c in advice
                   if all(c[m.role] == combo[m.role] for m in kept)}
    return [p for p in pkg.pins.values() if p.number not in blocked and p.sercoms & wanted]


def _check_bus(ref: str, pkg: Package, bus: str, group: list[Entry],
               blocked: frozenset[str]) -> list[str]:
    roles = {e.role for e in group}
    legal, advice = (_project(c, roles) for c in BUS_COMBOS[bus])
    if any(all(_fits(e, s, c) for e in group) for s in pkg.sercoms for c in legal):
        return []
    ranked = sorted(group, key=lambda e: PRIORITY[e.role])
    options = [(s, c) for s in pkg.sercoms for c in advice]

    def score(option: tuple) -> tuple:
        fits = tuple(_fits(e, *option) for e in ranked)
        return sum(fits), fits

    if not options:
        return [_w1(ref, e, ROLE_LABEL[e.role], ()) for e in group]
    best = max(score(o) for o in options)
    top = [o for o in options if score(o) == best]
    blamed = {e for e, fits in zip(ranked, best[1], strict=True) if not fits}
    return [_w1(ref, e, ROLE_LABEL[e.role], _candidates(pkg, e, group, top, advice, blocked))
            for e in group if e in blamed]


def _check_extint(ref: str, pkg: Package, ints: list[Entry],
                  blocked: frozenset[str]) -> list[str]:
    owner: dict[int, str] = {}                  # EXTINT number -> the net that has it
    bad: list[tuple[Entry, str]] = []
    for e in ints:
        line = e.pin.extint if e.pin is not None else None
        if line is None:
            bad.append((e, "PPS/INT (needs an EXTINT line)"))
        elif line in owner:
            bad.append((e, f"PPS/INT on EXTINT[{line}] (already used by net {owner[line]})"))
        else:
            owner[line] = e.net
    free = sorted((p for p in pkg.pins.values()
                   if p.extint is not None and p.extint not in owner and p.number not in blocked),
                  key=lambda p: (bool(p.sercoms), _pad_key(p.number)))   # keep SERCOM pins
    return [_w1(ref, e, role, free) for e, role in bad]


def _blocked(mine: Sequence[tuple[str, str]], group: Iterable[Entry]) -> frozenset[str]:
    """The MCU pads that carry a net outside ``group``."""
    nets = {e.net for e in group}
    return frozenset(pad for pad, net in mine if net not in nets)


def _entries(pkg: Package, mine: Sequence[tuple[str, str]]
             ) -> tuple[list[tuple[tuple[str, ...], Entry]], list[str]]:
    """((bus key, entry) per checked interface pad, pads outside the table)."""
    entries: list[tuple[tuple[str, ...], Entry]] = []
    unknown: list[str] = []
    for pad, net in mine:
        parsed = net_role(net)
        if parsed is None or parsed[0] == "SS":
            continue
        pin = pkg.pins.get(pad)
        label = pin.name if pin is not None else pkg.other.get(pad)
        if label is None:
            unknown.append(pad)
        else:
            entries.append((parsed[1], Entry(net, pad, parsed[0], label, pin)))
    return entries, list(dict.fromkeys(unknown))


def check_mcu(ref: str, pkg: Package, mine: Sequence[tuple[str, str]]
              ) -> tuple[list[str], list[str]]:
    """(PM-W1 warnings, pads outside the table) for one MCU's (pad, net) list."""
    entries, unknown = _entries(pkg, mine)
    groups: dict[tuple, list[Entry]] = {}
    for key, e in entries:
        if e.role in BUS_OF:
            groups.setdefault((BUS_OF[e.role], key), []).append(e)
    warnings: list[str] = []
    for (bus, _), group in groups.items():
        warnings += _check_bus(ref, pkg, bus, group, _blocked(mine, group))
    ints = [e for _, e in entries if e.role == "EXTINT"]
    return warnings + _check_extint(ref, pkg, ints, _blocked(mine, ints)), unknown


def _untabled_mcu(comp: Mapping[str, Any], lib_id: str) -> bool:
    """An MCU_* / RF_Module part that is not a fixed-function transceiver."""
    names = f"{lib_id} {comp.get('value', '')}"
    return bool(UNVERIFIED_LIBS.match(lib_id)) and not FIXED_FUNCTION_RADIOS.search(names)


def _unknown_note(ref: str, table: str, pads: list[str]) -> str:
    noun = "pad" if len(pads) == 1 else "pads"
    return f"{ref}: {noun} {', '.join(pads)} not in the {table} pin table"


def check(design: Mapping[str, Any], ctx: LintContext) -> tuple[list[str], list[str]]:
    """No errors; PM-W1 warnings per misplaced interface pin, then one PM-I0
    per lib_id whose interface pins could not be verified."""
    comps = design.get("components")
    comps = [c for c in comps if isinstance(c, Mapping)] if isinstance(comps, list) else []
    pads = _pads_by_ref(design.get("nets"))
    pkgs = packages(str(DATA_DIR))
    warnings: list[str] = []
    unverified: dict[str, list[str]] = {}       # lib_id -> refs (with notes)
    for comp in comps:
        ref, lib_id = str(comp.get("ref", "")), str(comp.get("lib_id", ""))
        mine = pads.get(ref, [])
        pkg = package_for(comp, pkgs)
        if pkg is None:
            if _untabled_mcu(comp, lib_id) and any(_roles(net)[0] for _, net in mine):
                unverified.setdefault(lib_id, []).append(ref)
            continue
        found, unknown = check_mcu(ref, pkg, mine)
        warnings += found
        if unknown:
            unverified.setdefault(lib_id, []).append(_unknown_note(ref, pkg.table, unknown))
    return [], warnings + [f"PM-I0 pin functions not verified for {lib} ({', '.join(refs)})"
                           for lib, refs in unverified.items()]
