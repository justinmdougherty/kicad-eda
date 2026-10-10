#!/usr/bin/env python3
"""Board fingerprint: one sha256 over a normalised view of a .kicad_pcb's copper and
placement, so repeated pipeline runs can be checked for identical routed results
(ROADMAP D5 determinism; pipeline/determinism_check.py uses it).

Compared (nets by NAME, never by number; every coordinate, width, size and drill
rounded to 1e-4 mm; each category sorted, so the order of items in the file never
matters):
  segments    net, layer, width, the two endpoints ordered lexicographically
  arcs        net, layer, width, the two endpoints ordered lexicographically, mid point
  vias        net, at, size, drill, layers, via type (through / blind / micro ...)
  footprints  reference, lib id, position, rotation (modulo 360), layer; the position
              is (at x y rot) or the KiCad 11 (transform (translate x y) (rotate r))
  zones       net, layers, rule area (keepout) or not, outline points in file order,
              filled polygons as (layer, point list sorted), the polygons sorted

Not compared: uuid / tstamp, generator / generator_version / version,
the title block, paper, general / setup / plot settings, the layer table, net numbers
and the net table itself, board graphics and text (gr_*, the Edge.Cuts outline included),
dimensions, groups, images, footprint contents (pads, fields other than the
reference, graphics, 3D models), zone settings (name, priority, clearance, thermal
relief, hatch, fill mode), locked flags, via tenting / capping / filling, embedded
fonts and files.

API: fingerprint(pcb) -> {"sha256", "counts": {category: items}}; normalised_view(pcb);
diff(a, b) -> the first differing items ("only in A" / "only in B", or what changed).
CLI: board_fingerprint.py <pcb> [<pcb2>]: one board prints its fingerprint as JSON; two
exit 0 when identical, 1 with the diff when not; 2 on an error.

venv only (sexpdata, through sexpr_read): the board is scanned once at the top level,
as net_equivalence does, and only the items above are parsed."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import sexpr_read as sr  # noqa: E402

CATEGORIES = ("segments", "arcs", "vias", "footprints", "zones")
DIGITS = 4                      # 1e-4 mm
DIFF_LIMIT = 10
USAGE = "usage: board_fingerprint.py <board.kicad_pcb> [<other.kicad_pcb>]"
_FOOTPRINTS = ("footprint", "module")
_CATEGORY = {"segment": "segments", "arc": "arcs", "via": "vias", "footprint": "footprints",
             "module": "footprints", "zone": "zones"}
# the footprint children the view needs (pads, graphics and other fields stay unparsed)
_FOOTPRINT_PART = re.compile(r'\(\s*(?:layer\b|at\b|transform\b|property\s+"?Reference"?\s'
                             r'|fp_text\s+reference\s)')
_NOT_VIA_TYPE = frozenset({"locked"})
Item = dict[str, Any]
View = dict[str, list[Item]]
Table = Mapping[int, str]


# --- scanning and small readers -----------------------------------------------------------
def _scan(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """(net table sources, [(head, source)]) of the top-level items the view uses; a
    footprint's source keeps only its header and the children _FOOTPRINT_PART names."""
    if sr.head_of(text) != "kicad_pcb":
        raise ValueError("not a KiCad board (expected '(kicad_pcb ...')")
    text = sr.restore_teardrop_parens(text)
    stack: list[int] = []
    nets: list[str] = []
    items: list[tuple[str, str]] = []
    top, parts = "", None
    for m in sr.TOKEN_RE.finditer(text, len(text) - len(text.lstrip(sr.LEADING))):
        if m.group() == "(":
            stack.append(m.start())
            if len(stack) == 2:
                head = sr.HEAD_RE.match(text, m.start())
                top = head.group(1) if head else ""
                name = sr.FP_NAME_RE.match(text, m.start()) if top in _FOOTPRINTS else None
                parts = [name.group(0) if name else f"({top}"] if top in _FOOTPRINTS else None
            continue
        if m.group() != ")":
            continue
        if not stack:
            raise ValueError(f"unbalanced parentheses: ')' at offset {m.start()} closes nothing")
        opened = stack.pop()
        if len(stack) == 2 and parts is not None and _FOOTPRINT_PART.match(text, opened):
            parts.append(text[opened:m.end()])
        elif len(stack) == 1:
            if top == "net":
                nets.append(text[opened:m.end()])
            elif top in _CATEGORY:
                items.append((top, " ".join(parts) + ")" if parts is not None
                               else text[opened:m.end()]))
            parts = None
    if stack:
        raise ValueError(f"unbalanced parentheses: '(' at offset {stack[-1]} is never closed")
    return nets, items


def _r(value: float) -> float:
    return round(value, DIGITS) + 0.0           # + 0.0: no -0.0


def _mm(token: Any) -> float:
    return _r(float(sr.atom(token)))


def _angle(token: Any) -> float:
    return _r(round(float(sr.atom(token)), DIGITS) % 360.0) % 360.0


def _point(node: list | None, what: str) -> list[float]:
    if node is None or len(node) < 3:
        raise ValueError(f"missing ({what} x y)")
    return [_mm(node[1]), _mm(node[2])]


def _size(node: list, name: str) -> float | None:
    found = sr.child(node, name)
    return _mm(found[1]) if found is not None and len(found) > 1 else None


def _net(node: list, table: Table) -> str:
    """The item's net NAME: (net_name "x") of a KiCad <= 9 zone, else (net N) through
    the board's net table, or (net "x") / (net N "x") as written."""
    name = sr.value(node, "net_name")
    if name:
        return sr.unescape_net_name(name)
    found = sr.child(node, "net")
    if found is None or len(found) < 2:
        return sr.NO_NET
    last = found[-1]
    if isinstance(last, int) and not isinstance(last, bool):
        if last in table:
            return table[last]
        if last == 0:
            return sr.NO_NET
        raise ValueError(f"references net {last}, which the board's net table does not define")
    return sr.unescape_net_name(sr.atom(last))


def _pts(node: list | None) -> list[list[float]]:
    """(pts (xy x y) ... (arc (start) (mid) (end))): [x, y] per xy, 6 numbers per arc."""
    out: list[list[float]] = []
    for p in (sr.child(node, "pts") or [])[1:]:
        if sr.tag(p) == "xy":
            out.append(_point(p, "xy"))
        elif sr.tag(p) == "arc":
            out.append([v for part in ("start", "mid", "end")
                        for v in _point(sr.child(p, part), part)])
    return out


# --- one normalised item per board item -----------------------------------------------------
def _segment(node: list, table: Table) -> Item:
    ends = sorted([_point(sr.child(node, "start"), "start"), _point(sr.child(node, "end"), "end")])
    return {"net": _net(node, table), "layer": sr.value(node, "layer"),
            "width": _size(node, "width"), "ends": ends}


def _arc(node: list, table: Table) -> Item:
    return {**_segment(node, table), "mid": _point(sr.child(node, "mid"), "mid")}


def _via(node: list, table: Table) -> Item:
    kinds = [sr.atom(t) for t in node[1:] if not isinstance(t, list)]
    layers = sr.child(node, "layers") or []
    return {"net": _net(node, table), "at": _point(sr.child(node, "at"), "at"),
            "size": _size(node, "size"), "drill": _size(node, "drill"),
            "layers": [sr.atom(x) for x in layers[1:]],
            "type": next((k for k in kinds if k not in _NOT_VIA_TYPE), "through")}


def _placement(node: list) -> tuple[list[float], float]:
    at = sr.child(node, "at")
    if at is not None:
        return _point(at, "at"), _angle(at[3] if len(at) > 3 else 0)
    transform = sr.child(node, "transform")
    if transform is None:
        raise ValueError("a footprint without (at ...) or (transform ...)")
    rotate = sr.child(transform, "rotate")
    return (_point(sr.child(transform, "translate"), "translate"),
            _angle(rotate[1] if rotate is not None and len(rotate) > 1 else 0))


def _reference(node: list) -> str:
    for head in ("property", "fp_text"):          # KiCad 8+ property wins over fp_text
        for c in sr.children(node, head):
            if len(c) >= 3 and sr.atom(c[1]) in ("Reference", "reference"):
                return sr.atom(c[2])
    return ""


def _footprint(node: list, _table: Table) -> Item:
    at, rot = _placement(node)
    lib = sr.atom(node[1]) if len(node) > 1 and not isinstance(node[1], list) else ""
    return {"ref": _reference(node), "lib_id": lib, "at": at, "rot": rot,
            "layer": sr.value(node, "layer")}


def _zone(node: list, table: Table) -> Item:
    layers = {sr.atom(x) for head in ("layer", "layers") for c in sr.children(node, head)
              for x in c[1:]}
    filled = [{"layer": sr.value(f, "layer"), "pts": sorted(_pts(f))}
              for f in sr.children(node, "filled_polygon")]
    return {"net": _net(node, table), "layers": sorted(layers),
            "keepout": sr.child(node, "keepout") is not None,
            "outline": [_pts(p) for p in sr.children(node, "polygon")],
            "filled": sorted(filled, key=_canon)}


_BUILDERS: dict[str, Callable[[list, Table], Item]] = {
    "segments": _segment, "arcs": _arc, "vias": _via, "footprints": _footprint,
    "zones": _zone}


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# --- the view, the fingerprint and the diff ---------------------------------------------------
def view_of_text(text: str) -> View:
    """The normalised view (see the module doc) of a board's text."""
    nets, items = _scan(text)
    table = sr.net_table(nets)
    found: dict[str, list[Item]] = {c: [] for c in CATEGORIES}
    for i, (head, src) in enumerate(items):
        category = _CATEGORY[head]
        try:
            node = sr.parse(sr.quote_bare_keys(src) if head in _FOOTPRINTS else src)
            found[category].append(_BUILDERS[category](node, table))
        except ValueError as e:
            raise ValueError(f"{head} (top-level item {i + 1}): {e}") from e
    return {c: sorted(found[c], key=_canon) for c in CATEGORIES}


def normalised_view(pcb: str | Path) -> View:
    path = Path(pcb)
    text = path.read_text(encoding="utf-8")
    try:
        return view_of_text(text)
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from e


def fingerprint_of_view(view: View) -> dict[str, Any]:
    digest = hashlib.sha256(_canon(view).encode("utf-8")).hexdigest()
    return {"sha256": digest, "counts": {c: len(view[c]) for c in CATEGORIES}}


def fingerprint(pcb: str | Path) -> dict[str, Any]:
    """{"sha256": hex digest of the normalised view, "counts": {category: items}}."""
    return fingerprint_of_view(normalised_view(pcb))


def _num(value: float) -> str:
    return f"{value:.{DIGITS}f}".rstrip("0").rstrip(".")


def _xy(point: list[float]) -> str:
    return f"({_num(point[0])}, {_num(point[1])})"


_FORMATS: dict[str, Callable[[Any], str]] = {
    "ends": lambda v: "-".join(_xy(p) for p in v), "at": _xy, "mid": _xy,
    "layers": "/".join,
    "outline": lambda v: f"{len(v)} outline(s), {sum(len(p) for p in v)} point(s)",
    "filled": lambda v: f"{len(v)} polygon(s), {sum(len(p['pts']) for p in v)} point(s)"}


def _fmt(key: str, value: Any) -> str:
    if key in _FORMATS:
        return _FORMATS[key](value)
    return _num(value) if isinstance(value, float) else str(value)


def _label(category: str, item: Item) -> str:
    net = item.get("net") or "(no net)"
    if category in ("segments", "arcs"):
        return (f"{category[:-1]} {net} {item['layer']} {_fmt('ends', item['ends'])} "
                f"width {_fmt('width', item['width'])}")
    if category == "vias":
        return f"via {net} at {_xy(item['at'])}"
    if category == "footprints":
        return f"footprint {item['ref'] or '(no reference)'} ({item['lib_id']})"
    return f"zone {net} on {_fmt('layers', item['layers'])}"


def _changes(a: Item, b: Item) -> str:
    out = []
    for key in a:
        if a[key] == b.get(key):
            continue
        was, now = _fmt(key, a[key]), _fmt(key, b.get(key))
        out.append(f"{key} differs ({was})" if was == now else f"{key} {was} -> {now}")
    return "; ".join(out)


def _only(items: list[Item], other: list[Item]) -> list[Item]:
    """items minus other, as multisets."""
    left = Counter(_canon(i) for i in other)
    out = []
    for item in items:
        key = _canon(item)
        if left[key]:
            left[key] -= 1
        else:
            out.append(item)
    return out


def _partner(category: str, item: Item, others: list[Item], used: set[int]) -> int | None:
    """The other side's item this one became: the same reference for a footprint,
    else an item that differs in exactly one field."""
    for j, other in enumerate(others):
        if j in used:
            continue
        if category == "footprints":
            if other["ref"] == item["ref"]:
                return j
        elif sum(item[k] != other.get(k) for k in item) == 1:
            return j
    return None


def _category_diff(category: str, a: list[Item], b: list[Item], room: int) -> list[str]:
    only_a, only_b = _only(a, b), _only(b, a)
    lines: list[str] = []
    used: set[int] = set()
    for item in only_a:
        if len(lines) >= room:
            return lines
        j = _partner(category, item, only_b, used)
        if j is None:
            lines.append(f"{_label(category, item)}: only in A")
        else:
            used.add(j)
            lines.append(f"{_label(category, item)}: {_changes(item, only_b[j])}")
    lines += [f"{_label(category, it)}: only in B" for j, it in enumerate(only_b) if j not in used]
    return lines[:room]


def diff_views(a: View, b: View, limit: int = DIFF_LIMIT) -> list[str]:
    """At most `limit` lines: the categories whose counts differ, then the differing
    items category by category; [] when the views are identical."""
    lines = [f"{c}: {len(a[c])} in A, {len(b[c])} in B" for c in CATEGORIES
             if len(a[c]) != len(b[c])]
    for c in CATEGORIES:
        if len(lines) >= limit:
            break
        lines += _category_diff(c, a[c], b[c], limit - len(lines))
    return lines[:limit]


def diff(a_path: str | Path, b_path: str | Path, limit: int = DIFF_LIMIT) -> list[str]:
    """A short human list of the first items in which two boards differ (A = a_path)."""
    return diff_views(normalised_view(a_path), normalised_view(b_path), limit)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    asked = args[:1] in (["-h"], ["--help"])
    if asked or not 1 <= len(args) <= 2 or args[0].startswith("-"):
        print(f"{USAGE}\n{__doc__}" if asked else USAGE, file=sys.stdout if asked else sys.stderr)
        return 0 if asked else 2
    try:
        views = [normalised_view(p) for p in args]
    except (OSError, ValueError, RuntimeError) as e:
        print(f"board_fingerprint: {e}", file=sys.stderr)
        return 2
    prints = [fingerprint_of_view(v) for v in views]
    if len(args) == 1:
        print(json.dumps({"path": args[0], **prints[0]}))
        return 0
    for path, fp in zip(args, prints, strict=True):
        print(f"{fp['sha256']}  {path}")
    if prints[0]["sha256"] == prints[1]["sha256"]:
        print("identical")
        return 0
    print("different (A = first, B = second):")
    print("\n".join(f"  {line}" for line in diff_views(*views)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
