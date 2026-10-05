#!/usr/bin/env python3
"""Schematic side of the KiCad project importer (ROADMAP_DECISIONS D4).

Two inputs, both read without KiCad:

1. the ``kicad-cli sch export netlist --format kicadxml`` export
   (``parse_netlist_xml``): components with their value, footprint field,
   user fields, DNP / exclude flags, library source and sheet; the library
   parts' pin lists; the nets with each node's pin type (``+no_connect`` =
   the pin carries a no-connect flag). The XML is unescaped, unlike the
   S-expression export (a ``/`` in a label stays ``/``).
2. the ``.kicad_sch`` files themselves, for the symbols KiCad embedded in
   each sheet's ``(lib_symbols ...)`` (``read_sheets`` / ``embedded_symbols``):
   the exact definitions the design uses, local ``lib_name`` copies included.
   ``symbol_library_text`` turns them into a ``.kicad_sym`` library.

Symbol text is handled as text (spans + renames), never re-serialised, so
escapes, numbers and formatting survive byte for byte.

Sheet names (``read_sheets``) are the files as KiCad resolves them: a
relative Sheetfile against the referencing sheet's directory, else against
the project directory (checked with kicad-cli 9 and 10.99), named relative
to the root sheet's directory (``..`` allowed) or, when referenced by an
absolute path, absolute.
"""
from __future__ import annotations

import os
import posixpath
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import kicad_dialect as kd  # noqa: E402
import net_equivalence as ne  # noqa: E402
import sexpr_read as sr  # noqa: E402

FLAG_DNP, FLAG_NO_BOM, FLAG_NO_BOARD = "dnp", "exclude_from_bom", "exclude_from_board"
_FLAGS = frozenset({FLAG_DNP, FLAG_NO_BOM, FLAG_NO_BOARD})
NO_CONNECT_SUFFIX = "+no_connect"
# KiCad 9's schematic format; anything newer needs KiCad 10+ to load
K9_SCH_FORMAT = 20250114
# the .kicad_sym dialect the extracted symbols are read in: KiCad 9's for a
# KiCad 9 schematic (D0), KiCad 10's for anything newer (write_symbol_library
# then writes them in KiCad 9's through kicad_dialect when it can)
K9_SYM_FORMAT, K10_SYM_FORMAT = 20241209, 20251024
_SHEETFILE_NAMES = ("Sheetfile", "Sheet file")      # KiCad 7+ / KiCad 6
_SYMBOL_NAME = re.compile(r'\(\s*symbol\s+"((?:[^"\\]|\\.)*)"')
_VERSION = re.compile(r"\(\s*version\s+(\d+)\s*\)")
STAGE_UP = "_up"            # stage_sheets: the root copy's parent dirs for '../' sheets


# ----------------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------------
def io_path(path: str | Path) -> Path:
    """``path`` made absolute and usable for Python file I/O: on Windows in
    its extended-length form (``\\\\?\\C:\\...``, ``\\\\?\\UNC\\server\\...``),
    which lifts MAX_PATH for this process, so long footprint names in a deep
    output directory can be written, listed and removed, even below a short
    directory. KiCad itself is not helped: kicad_import_fplib.too_long_for_kicad
    reports those files. Only for I/O calls; report paths stay plain."""
    full = os.path.abspath(str(path))
    if sys.platform != "win32" or full.startswith("\\\\?\\"):
        return Path(full)
    if full.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + full[2:])
    return Path("\\\\?\\" + full)


def is_absolute_ref(ref: str) -> bool:
    """A Sheetfile that is absolute on any platform (``/x``, ``C:/x``, ``\\\\server\\x``)."""
    return ref.startswith(("/", "\\")) or bool(PureWindowsPath(ref).drive)


@dataclass(frozen=True)
class SchPin:
    number: str
    name: str
    type: str


@dataclass(frozen=True)
class SchComponent:
    ref: str
    value: str
    footprint: str
    datasheet: str
    description: str
    lib: str
    part: str
    fields: tuple[tuple[str, str], ...]
    flags: frozenset[str]
    sheet_names: str
    sheet_file: str

    @property
    def symbol_key(self) -> str:
        """The name KiCad embedded the symbol under: ``lib:part``, or the bare
        ``part`` of a locally renamed (``lib_name``) copy."""
        return f"{self.lib}:{self.part}" if self.lib else self.part

    @property
    def dnp(self) -> bool:
        return FLAG_DNP in self.flags


@dataclass(frozen=True)
class SchNode:
    ref: str
    pin: str
    function: str
    type: str

    @property
    def flagged_nc(self) -> bool:
        return self.type.endswith(NO_CONNECT_SUFFIX)


@dataclass(frozen=True)
class SchNet:
    name: str
    nodes: tuple[SchNode, ...]


@dataclass(frozen=True)
class SchDesign:
    tool: str
    source: str
    title: str
    components: tuple[SchComponent, ...]
    libparts: Mapping[tuple[str, str], tuple[SchPin, ...]]
    nets: tuple[SchNet, ...]
    libraries: Mapping[str, str]

    def component(self, ref: str) -> SchComponent | None:
        return next((c for c in self.components if c.ref == ref), None)

    def pins_of(self, comp: SchComponent) -> tuple[SchPin, ...]:
        return self.libparts.get((comp.lib, comp.part), ())


# ----------------------------------------------------------------------------
# XML netlist
# ----------------------------------------------------------------------------
def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _component(comp: ET.Element) -> SchComponent:
    ref = comp.get("ref", "")
    if not ref:
        raise ValueError("a <comp> without ref")
    src = comp.find("libsource")
    props = {p.get("name", ""): p.get("value", "") for p in comp.findall("property")}
    fields = tuple((f.get("name", ""), _text(f)) for f in comp.findall("fields/field")
                   if f.get("name"))
    sheet = comp.find("sheetpath")
    return SchComponent(
        ref=ref, value=_text(comp.find("value")), footprint=_text(comp.find("footprint")),
        datasheet=_text(comp.find("datasheet")), description=_text(comp.find("description")),
        lib=src.get("lib", "") if src is not None else "",
        part=src.get("part", "") if src is not None else "",
        fields=fields, flags=frozenset(props) & _FLAGS,
        sheet_names=sheet.get("names", "/") if sheet is not None else "/",
        sheet_file=props.get("Sheetfile", ""))


def _libparts(root: ET.Element) -> dict[tuple[str, str], tuple[SchPin, ...]]:
    out: dict[tuple[str, str], tuple[SchPin, ...]] = {}
    for lp in root.iterfind("libparts/libpart"):
        pins = tuple(SchPin(p.get("num", ""), p.get("name", ""), p.get("type", ""))
                     for p in lp.findall("pins/pin") if p.get("num"))
        out[(lp.get("lib", ""), lp.get("part", ""))] = pins
    return out


def parse_netlist_xml(text: str) -> SchDesign:
    """A ``--format kicadxml`` export as a SchDesign. Raises ValueError on
    anything that is not a KiCad XML netlist."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"not well-formed XML: {e}") from e
    if root.tag != "export":
        raise ValueError(f"not a KiCad netlist export (root <{root.tag}>, expected <export>)")
    if root.find("nets") is None or root.find("components") is None:
        raise ValueError("the XML netlist lacks <components> or <nets>")
    nets = []
    for net in root.iterfind("nets/net"):
        nodes = tuple(SchNode(n.get("ref", ""), n.get("pin", ""), n.get("pinfunction", ""),
                              n.get("pintype", "")) for n in net.findall("node"))
        nets.append(SchNet(net.get("name") or f"<net code {net.get('code', '?')}>", nodes))
    design = root.find("design")
    title = ""
    if design is not None:
        first = design.find("sheet")
        title = _text(first.find("title_block/title")) if first is not None else ""
    return SchDesign(
        tool=_text(root.find("design/tool")), source=_text(root.find("design/source")),
        title=title, components=tuple(_component(c) for c in root.iterfind("components/comp")),
        libparts=_libparts(root), nets=tuple(nets),
        libraries={lib.get("logical", ""): _text(lib.find("uri"))
                   for lib in root.iterfind("libraries/library")})


# ----------------------------------------------------------------------------
# .kicad_sch text: spans of children
# ----------------------------------------------------------------------------
def child_spans(text: str, start: int, end: int) -> list[tuple[str, int, int]]:
    """(head, start, end) of every direct child list of the node spanning
    text[start:end] (which must start with its own '(')."""
    out: list[tuple[str, int, int]] = []
    depth, opened = 0, 0
    for m in sr.TOKEN_RE.finditer(text, start, end):
        tok = m.group()
        if tok == "(":
            depth += 1
            if depth == 2:
                opened = m.start()
        elif tok == ")":
            if depth == 2:
                head = sr.HEAD_RE.match(text, opened)
                out.append((head.group(1) if head else "", opened, m.end()))
            depth -= 1
            if depth == 0:
                break
    return out


def _root_span(text: str, head: str, what: str) -> tuple[int, int]:
    start = len(text) - len(text.lstrip(sr.LEADING))
    if sr.head_of(text) != head:
        raise ValueError(f"{what} is not a '({head} ...' file")
    depth = 0
    for m in sr.TOKEN_RE.finditer(text, start):
        tok = m.group()
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
            if depth == 0:
                return start, m.end()
    raise ValueError(f"{what}: unbalanced parentheses")


def schematic_version(text: str) -> int | None:
    m = _VERSION.search(text, 0, 4096)
    return int(m.group(1)) if m else None


def _sheet_files(text: str, spans: list[tuple[str, int, int]]) -> list[str]:
    """Sub-sheet file names referenced by a schematic's ``(sheet ...)`` items."""
    out = []
    for head, s, e in spans:
        if head != "sheet":
            continue
        for phead, ps, pe in child_spans(text, s, e):
            if phead != "property":
                continue
            node = sr.parse(text[ps:pe])
            if len(node) >= 3 and sr.atom(node[1]) in _SHEETFILE_NAMES:
                out.append(sr.atom(node[2]))
    return out


def sheet_name(base: Path, parent: str, ref: str) -> str:
    """The read_sheets name of the Sheetfile ``ref`` found in sheet
    ``parent``: absolute stays absolute; a relative one resolves against
    the parent's directory, else (KiCad's fallback) the project directory
    ``base``. Raises ValueError when neither file exists."""
    ref = ref.replace("\\", "/")
    if is_absolute_ref(ref):
        if not (base / ref).is_file():
            raise ValueError(f"schematic sheet {ref} (referenced from {parent}) not found")
        return posixpath.normpath(ref)
    first = posixpath.normpath(posixpath.join(posixpath.dirname(parent), ref))
    second = posixpath.normpath(ref)
    for cand in (first, second):
        if (base / cand).is_file():
            return cand
    tried = " or ".join(dict.fromkeys((first, second)))
    raise ValueError(f"schematic sheet {ref} (referenced from {parent}) not found: no {tried} "
                     f"under {base}")


def read_sheets(root_sch: str | Path) -> list[tuple[str, str]]:
    """(name, text) of the root schematic and every sub-sheet it reaches,
    each file once, root first (names: see the module doc). A referenced
    sheet that does not exist fails loudly."""
    root = Path(root_sch)
    base = root.parent
    order: list[tuple[str, str]] = []
    seen: set[str] = set()
    todo = [root.name]
    while todo:
        name = todo.pop(0)
        key = os.path.normcase(name)
        if key in seen:
            continue
        seen.add(key)
        path = base / name
        if not path.is_file():
            raise ValueError(f"schematic {name} not found in {base}")
        text = path.read_text(encoding="utf-8")
        s, e = _root_span(text, "kicad_sch", str(path))
        order.append((name, text))
        todo += [sheet_name(base, name, ref) for ref in _sheet_files(text, child_spans(text, s, e))]
    return order


@dataclass(frozen=True)
class EmbeddedSymbol:
    key: str          # the name in lib_symbols ("Device:R" or a lib_name copy)
    sheet: str        # the sheet file it was read from
    text: str         # the (symbol ...) source, verbatim


def embedded_symbols(sheets: Iterable[tuple[str, str]]) -> dict[str, dict[str, EmbeddedSymbol]]:
    """{sheet file: {key: EmbeddedSymbol}} from each sheet's lib_symbols."""
    out: dict[str, dict[str, EmbeddedSymbol]] = {}
    for name, text in sheets:
        s, e = _root_span(text, "kicad_sch", name)
        found: dict[str, EmbeddedSymbol] = {}
        for head, ls, le in child_spans(text, s, e):
            if head != "lib_symbols":
                continue
            for shead, ss, se in child_spans(text, ls, le):
                m = _SYMBOL_NAME.match(text, ss)
                if shead == "symbol" and m:
                    found[m.group(1)] = EmbeddedSymbol(m.group(1), name, text[ss:se])
        out[name] = found
    return out


def bare_name(key: str) -> str:
    return key.rpartition(":")[2]


def _normalised(text: str) -> str:
    return " ".join(text.split())


def same_symbol(a: EmbeddedSymbol, b: EmbeddedSymbol) -> bool:
    """Equal definitions (whitespace aside)."""
    return _normalised(a.text) == _normalised(b.text)


def rename_symbol(sym: EmbeddedSymbol, new: str) -> str:
    """The symbol's text with its top name set to ``new`` and every unit
    sub-symbol ``<old bare>_<unit>_<style>`` renamed ``<new>_<unit>_<style>``
    (symlib reads the unit from that suffix)."""
    old = bare_name(sym.key)
    out, pos = [], 0
    for i, m in enumerate(_SYMBOL_NAME.finditer(sym.text)):
        name = m.group(1)
        if i == 0:
            repl = new
        elif name.startswith(old + "_"):
            repl = new + name[len(old):]
        else:
            raise ValueError(f"symbol {sym.key} ({sym.sheet}): unit {name!r} does not start "
                             f"with {old + '_'!r}")
        out += [sym.text[pos:m.start(1)], repl]
        pos = m.end(1)
    out.append(sym.text[pos:])
    return "".join(out)


def symbol_library_text(symbols: Iterable[tuple[str, EmbeddedSymbol]], fmt: int) -> str:
    """A .kicad_sym library holding each (new name, embedded symbol)."""
    body = []
    for new, sym in symbols:
        if '"' in new or "\\" in new or ":" in new:
            raise ValueError(f"cannot name a library symbol {new!r}")
        body.append("\t" + rename_symbol(sym, new))
    head = (f'(kicad_symbol_lib\n\t(version {fmt})\n\t(generator "kicad-eda")\n'
            f'\t(generator_version "{"9.0" if fmt <= K9_SYM_FORMAT else "10.0"}")\n')
    return head + "\n".join(body) + "\n)\n"


def symbol_format_for(sch_version: int | None) -> int:
    """The .kicad_sym dialect matching a schematic format (see module doc)."""
    return K9_SYM_FORMAT if sch_version is None or sch_version <= K9_SCH_FORMAT else K10_SYM_FORMAT


def has_extends(sym: EmbeddedSymbol) -> bool:
    """KiCad embeds symbols flattened; an ``(extends ...)`` here cannot be
    resolved inside the extracted library."""
    s, e = 0, len(sym.text)
    return any(head == "extends" for head, _a, _b in child_spans(sym.text, s, e))


_BAD_SYMBOL_CHARS = re.compile(r'["\\:\x00-\x1f]')


@dataclass(frozen=True)
class SymbolPlan:
    """How the embedded symbols become one library: ``names`` {ref: symbol
    name in it}, ``entries`` the (name, symbol) pairs to write, ``missing``
    the refs whose symbol no sheet embeds, ``variants`` the symbols named
    other than their bare name, ``problems`` what cannot be carried."""
    names: Mapping[str, str]
    entries: tuple[tuple[str, EmbeddedSymbol], ...]
    missing: tuple[str, ...]
    variants: tuple[Mapping[str, str], ...]
    problems: tuple[str, ...]


def _lookup(comp: SchComponent, embedded: Mapping[str, Mapping[str, EmbeddedSymbol]]
            ) -> EmbeddedSymbol | None:
    """The definition the component's own sheet embeds, else any sheet's."""
    own = embedded.get(comp.sheet_file, {}).get(comp.symbol_key)
    if own is not None:
        return own
    return next((syms[comp.symbol_key] for syms in embedded.values()
                 if comp.symbol_key in syms), None)


def _new_name(sym: EmbeddedSymbol, taken: Mapping[str, EmbeddedSymbol]) -> str:
    lib, _, bare = sym.key.rpartition(":")
    bare = _BAD_SYMBOL_CHARS.sub("_", bare) or "symbol"
    variant = any(s.key == sym.key for s in taken.values())     # a sheet-local edit
    candidates = [bare] + ([_BAD_SYMBOL_CHARS.sub("_", f"{lib}_{bare}")]
                           if lib and not variant else [])
    for name in candidates:
        if name not in taken:
            return name
    n = 2
    while f"{bare}_{n}" in taken:
        n += 1
    return f"{bare}_{n}"


def plan_symbols(components: Iterable[SchComponent],
                 embedded: Mapping[str, Mapping[str, EmbeddedSymbol]]) -> SymbolPlan:
    """Name every component's embedded symbol in the extracted library: the
    bare name (``Device:R`` -> ``R``) unless another definition took it, then
    ``<lib>_<bare>`` (another library's symbol of that name), then
    ``<bare>_2``...; identical definitions share one entry, a sheet-local
    edit of the same lib_id gets its own numbered name."""
    names: dict[str, str] = {}
    taken: dict[str, EmbeddedSymbol] = {}
    missing, variants, problems = [], [], []
    for comp in components:
        sym = _lookup(comp, embedded)
        if sym is None:
            missing.append(comp.ref)
            continue
        name = next((n for n, s in taken.items() if s.key == sym.key and same_symbol(s, sym)),
                    None)
        if name is None:
            name = _new_name(sym, taken)
            taken[name] = sym
            if name != bare_name(sym.key):
                variants.append({"name": name, "key": sym.key, "sheet": sym.sheet})
            if has_extends(sym):
                problems.append(f"symbol {sym.key} ({sym.sheet}) still uses (extends ...); the "
                                "extracted library cannot resolve its base")
        names[comp.ref] = name
    return SymbolPlan(names=names, entries=tuple(taken.items()), missing=tuple(missing),
                      variants=tuple(variants), problems=tuple(problems))


def verify_symbols(path: Path) -> dict:
    """KiCad 9's own load check of a written symbol library (kicad_dialect.k9_check)."""
    return kd.k9_check(path, "sym")


def _in_kicad9_dialect(text: str, fmt: int, info: dict) -> tuple[str, dict]:
    """(text to write, report entry) for a library newer than KiCad 9's:
    kicad_dialect's KiCad 9 text, or, when it refuses a symbol (D0 needs
    every one), the newer text and the refused constructs."""
    res = kd.downgrade_symbol_library(text)
    if res.ok:
        return res.text, {**info, "format": K9_SYM_FORMAT, "d0_dialect": True,
                          "downgraded_from": fmt, "downgrade": {
                              "dropped": {c.rule: c.count for c in res.dropped},
                              "rewritten": {c.rule: c.count for c in res.rewritten}}}
    refused = ([{"item": n, "construct": c, "reason": r} for n, c, r in res.refused_items]
               or [{"item": "(library)", "construct": res.construct or "",
                    "reason": res.refused or ""}])
    constructs = ", ".join(sorted({r["construct"] for r in refused}))
    return text, {**info, "d0_dialect": False, "refused": refused, "kicad11_path_required": (
        f"the schematic is newer than KiCad 9 and kicad_dialect refused {constructs} "
        f"({len(refused)} symbol(s)); its symbols are written as format {fmt}, which KiCad 9 "
        "cannot load")}


def _k9_checked(path: Path, info: dict) -> dict:
    check = verify_symbols(path)
    summary = {k: check[k] for k in ("loaded", "compared", "reason") if k in check}
    summary.update({k: len(check[k]) for k in ("differing", "missing") if k in check})
    summary["errors"] = list(check.get("errors", ()))[:3]
    return {**info, "k9_verified": check.get("ok"), "k9_check": summary}


def write_symbol_library(components: Iterable[SchComponent], sheets: list[tuple[str, str]],
                         path: Path) -> tuple[SymbolPlan, dict]:
    """``path`` (.kicad_sym) holding the embedded symbol of every component
    (read_sheets output for ``sheets``); returns the plan and its report
    entry. A schematic newer than KiCad 9's gives KiCad 9 symbols
    (kicad_dialect, then KiCad 9's own load check: ``k9_verified``) unless a
    symbol has no KiCad 9 form (``refused``, ``kicad11_path_required``).
    Nothing is written when no symbol was found."""
    plan = plan_symbols(components, embedded_symbols(sheets))
    fmt = symbol_format_for(schematic_version(sheets[0][1]))
    info: dict = {"file": str(path), "format": fmt, "symbols": len(plan.entries),
                  "variants": list(plan.variants), "missing": list(plan.missing),
                  "problems": list(plan.problems), "d0_dialect": fmt <= K9_SYM_FORMAT}
    if not plan.entries:
        return plan, info
    text = symbol_library_text(plan.entries, fmt)
    if fmt > K9_SYM_FORMAT:
        text, info = _in_kicad9_dialect(text, fmt, info)
    io_path(path.parent).mkdir(parents=True, exist_ok=True)
    io_path(path).write_text(text, encoding="utf-8", newline="\n")
    if fmt > K9_SYM_FORMAT and info["d0_dialect"]:
        info = _k9_checked(path, info)
    return plan, info


# ----------------------------------------------------------------------------
# kicad-cli export
# ----------------------------------------------------------------------------
def choose_kicad_cli(sch_version: int | None) -> tuple[str, str]:
    """(kicad-cli path, version) for the schematic export: the oldest
    install >= 9.0 for a KiCad 9 schematic (format <= 20250114), the newest
    (the 10.99 nightly when present) for anything newer."""
    import kicad_env as env
    found = env.discover()
    if not found:
        raise RuntimeError("no kicad-cli found: install KiCad (or set KICAD_PATH), or pass the "
                           "schematic netlist export as netlist_xml")
    if sch_version is None or sch_version <= K9_SCH_FORMAT:
        able = [i for i in found if i.version_tuple >= (9, 0, 0)] or found
        pick = min(able, key=lambda i: i.version_tuple)
    else:
        pick = max(found, key=lambda i: i.version_tuple)
        if pick.version_tuple < (10, 0, 0):
            raise RuntimeError(f"the schematic is format {sch_version} (KiCad 10+); the newest "
                               f"kicad-cli here is {pick.version}: install the KiCad 11 nightly")
    return str(pick.kicad_cli), pick.version


def _leading_ups(name: str) -> int:
    n = 0
    for part in name.split("/"):
        if part != "..":
            break
        n += 1
    return n


def stage_sheets(root: Path, names: list[str], extra: Iterable[Path], src: Path) -> Path:
    """Copy the sheets (read_sheets names, root first) and ``extra`` files
    (the .kicad_pro) under ``src`` with their relative layout intact, so
    every Sheetfile still resolves in the copy; returns the root's copy.
    The root goes as many ``_upN`` directories deep as the most ``../``
    any sheet name has, so ``../../x`` lands inside ``src`` too. A sheet
    named by an absolute path is not copied: kicad-cli reads it in place
    and writes nothing beside a sub-sheet."""
    relative = [n for n in names if not is_absolute_ref(n)]
    depth = max((_leading_ups(n) for n in relative), default=0)
    home = src.joinpath(*(f"{STAGE_UP}{i}" for i in range(1, depth + 1)))
    for name in relative:
        dst = Path(os.path.normpath(home / name))
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, dst)
    for path in extra:
        shutil.copy2(path, home / path.name)
    return home / names[0]


def export_netlist_xml(root: Path, sheets: Iterable[tuple[str, str]], extra: Iterable[Path],
                       kicad_cli: str, work: Path) -> str:
    """The kicadxml export of a COPY of the schematic sheets (read_sheets
    output) plus ``extra`` files such as the .kicad_pro, staged in ``work``
    (stage_sheets): kicad-cli may write a .kicad_prl beside the schematic,
    and the source project stays untouched."""
    top = stage_sheets(root, [name for name, _text in sheets], extra, work / "src")
    xml = ne.export_schematic_xml(top, work / f"{top.stem}.xml", kicad_cli)
    return xml.read_text(encoding="utf-8")
