#!/usr/bin/env python3
"""DSN layer policy for Freerouting (D7-19, ADR D7.9).

KiCad's Specctra export lets Freerouting route signals across plane layers.
postprocess(dsn_text, policy) -> (text, report) makes it honour the roles:
- ``(type power)`` layers become ``(type signal)`` (typed power, plane-net SMD
  pads stay unconnected; ``use_layer`` keeps other nets off the planes);
- every non-plane class gets ``(circuit (use_layer <signal + mixed layers>))``;
- each plane net gets its own class ``kicad_eda_plane_<net>``: the signal
  layers plus its plane layer(s) (``plane_classes: "restricted"``, the D7
  variant) or no ``use_layer`` (``"unrestricted"``, the T10 fallback);
- each impedance net gets its own class ``kicad_eda_net_<net>`` at its primary
  width on the primary layer plus every routable layer whose solved width lies
  in the primary's window; other allowed layers join with their own widths
  only when per-class ``(layer_rule L (rule (width W)))`` is honoured
  (LAYER_RULE_VERIFIED, one recorded Freerouting 1.9.0 run);
- unused layers (single-sided B.Cu) are in no ``use_layer``.

The policy is the physical-design artifact's ``dsn`` section (ADR D7.4):
``{signal_layers (signal + mixed), plane_nets {net: [layers]}, unused_layers,
net_layers {net: {primary, window [lo, hi] mm, widths {layer: mm}}}}`` plus
the optional ``plane_classes``. Every DSN layer must be signal, plane or
unused and every named layer must exist (a stale artifact is refused). The
report: variant, source, retyped, classes, nets_moved, routed_layers,
layer_rules, excluded_layers, pad_layer_excluded (impedance nets whose SMD pad
layer, B-side parts mirrored, is outside their layers: autoroute_board refuses
them), pad_layer_blocked (the same for other nets: a warning), unused_layers,
layer_rule_verified, layer_rule_evidence, warnings. One-pin nets are in neither.

derive_policy_from_dsn(text): planes from ``(type power)`` layers or inner
layers carrying a ``(plane ...)``, never from outer pours (E10: KiCad exports
every zone as a plane); a power layer with no pour is unused.

DSN text is read and edited only through dsn_sexpr's span API (no regexes):
every byte outside the edited spans stays as KiCad wrote it (CRLF kept) and
class members keep KiCad's own token text. Stdlib only, Python 3.11
(swig_export_dsn.py runs under KiCad 9's bundled Python).
"""
from __future__ import annotations

import dataclasses
import json
import math
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import dsn_sexpr as ds

RESTRICTED, UNRESTRICTED = "restricted", "unrestricted"
VARIANTS = (RESTRICTED, UNRESTRICTED)
PLANE_CLASS_PREFIX = "kicad_eda_plane_"
NET_CLASS_PREFIX = "kicad_eda_net_"
POLICY_KEYS = frozenset({"signal_layers", "plane_nets", "unused_layers", "net_layers",
                         "plane_classes", "source", "notes"})
WINDOW_MIN_MM = 0.005               # D7.6: DRU window = solved width +- max(5 um, 1.5 %)
WINDOW_PCT = 1.5
WRAP_COLUMNS = 90                   # class member lists wrap like KiCad's
_EPS_MM = 1e-9
_UNITS_PER_MM = {"um": 1000.0, "mm": 1.0, "cm": 0.1, "mil": 1000.0 / 25.4, "inch": 1.0 / 25.4}
_CLASS_SAFE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_+.")

# The one-time check of ADR D7.9 (D7-19); tests/test_dsn_layers_live.py re-runs it.
LAYER_RULE_VERIFIED = True
LAYER_RULE_EVIDENCE = (
    "Freerouting 1.9.0, 2026-10-09, 6L fixture: 16 nets on F.Cu + In2.Cu, class width 250 um, "
    "(layer_rule In2.Cu (rule (width 150))): SES F.Cu wires 41/41 at 250 um, In2.Cu 14/14 at "
    "150 um; control without layer_rule: In2.Cu 14/14 at 250 um; KiCad 9.0.4 SES import keeps "
    "them (tests/fixtures/dsn_layers/layer_rule_evidence.json)")


class DsnLayersError(ValueError):
    """A malformed DSN, a bad policy, or a policy that does not fit the DSN."""


# --- the policy --------------------------------------------------------------
@dataclass(frozen=True)
class NetLayers:
    """An impedance net's allowed layers: solved width (mm) per layer, the
    primary layer and its DRU window (mm)."""
    primary: str
    window: tuple[float, float]
    widths: Mapping[str, float]


@dataclass(frozen=True)
class Policy:
    signal_layers: tuple[str, ...]
    plane_nets: Mapping[str, tuple[str, ...]]
    unused_layers: tuple[str, ...]
    net_layers: Mapping[str, NetLayers]
    plane_classes: str = RESTRICTED
    source: str = "artifact"
    ignored_keys: tuple[str, ...] = ()


def _names(value: object, what: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise DsnLayersError(f"{what} must be a list of layer names, got {value!r}")
    if not all(isinstance(x, str) and x for x in value) or len(set(value)) != len(value):
        raise DsnLayersError(f"{what} must hold distinct non-empty names, got {list(value)!r}")
    return tuple(value)


def _plane_nets(value: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        raise DsnLayersError(f"plane_nets must map a net to its plane layers, got {value!r}")
    out = {str(net): _names(layers, f"plane_nets[{net!r}]") for net, layers in value.items()}
    empty = [net for net, layers in out.items() if not layers]
    if empty:
        raise DsnLayersError(f"plane_nets {empty} name no layer")
    return out


def _width(net: str, layer: object, value: object) -> float:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    if not ok or not math.isfinite(value) or value <= 0:
        raise DsnLayersError(f"net {net}: widths[{layer!r}] must be a positive number of mm, "
                             f"got {value!r}")
    return float(value)


def default_window(width_mm: float) -> tuple[float, float]:
    """The DRU width window of a solved width (ADR D7.6): +- max(5 um, 1.5 %)."""
    half = max(WINDOW_MIN_MM, width_mm * WINDOW_PCT / 100.0)
    return (width_mm - half, width_mm + half)


def _window(net: str, value: object, primary_width: float) -> tuple[float, float]:
    if value is None:
        return default_window(primary_width)
    pair = list(value) if isinstance(value, Sequence) and not isinstance(value, str) else []
    if len(pair) != 2 or not all(isinstance(x, (int, float)) for x in pair) or pair[0] > pair[1]:
        raise DsnLayersError(f"net {net}: window must be [lo, hi] in mm, got {value!r}")
    lo, hi = float(pair[0]), float(pair[1])
    if not lo - _EPS_MM <= primary_width <= hi + _EPS_MM:
        raise DsnLayersError(f"net {net}: the window [{lo}, {hi}] does not contain the primary "
                             f"width {primary_width}")
    return (lo, hi)


def _net_entry(net: str, entry: object) -> NetLayers:
    if not isinstance(entry, Mapping) or not isinstance(entry.get("widths"), Mapping):
        raise DsnLayersError(f"net_layers[{net!r}] must be {{primary, widths[, window]}}, "
                             f"got {entry!r}")
    primary = entry.get("primary")
    widths = {str(k): _width(net, k, v) for k, v in entry["widths"].items()}
    if not isinstance(primary, str) or primary not in widths:
        raise DsnLayersError(f"net {net}: primary layer {primary!r} has no solved width in "
                             f"widths {sorted(widths)}")
    return NetLayers(primary, _window(net, entry.get("window"), widths[primary]), widths)


def load_policy(raw: object) -> Policy:
    """The checked Policy of an artifact's ``dsn`` section (module doc);
    DsnLayersError names what is wrong. Never mutates ``raw``."""
    if isinstance(raw, Policy):
        return raw
    if not isinstance(raw, Mapping):
        raise DsnLayersError(f"a layer policy is a mapping (the artifact's 'dsn' section), "
                             f"got {type(raw).__name__}")
    if "signal_layers" not in raw:
        raise DsnLayersError("the layer policy has no signal_layers")
    signal = _names(raw["signal_layers"], "signal_layers")
    if not signal:
        raise DsnLayersError("signal_layers is empty: no net could route")
    nets = raw.get("net_layers") or {}
    if not isinstance(nets, Mapping):
        raise DsnLayersError(f"net_layers must map a net to its layers, got {nets!r}")
    variant = raw.get("plane_classes", RESTRICTED)
    if variant not in VARIANTS:
        raise DsnLayersError(f"plane_classes must be one of {list(VARIANTS)}, got {variant!r}")
    return Policy(signal, _plane_nets(raw.get("plane_nets") or {}),
                  _names(raw.get("unused_layers") or [], "unused_layers"),
                  {str(n): _net_entry(str(n), e) for n, e in nets.items()}, variant,
                  str(raw.get("source", "artifact")),
                  tuple(sorted(str(k) for k in raw if k not in POLICY_KEYS)))


def load_policy_file(path: str | os.PathLike) -> dict:
    """The layer policy in a JSON file: the ``dsn`` section of a
    physical-design artifact (``<stem>.stackup.json``) or a bare policy."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise DsnLayersError(f"cannot read the layer policy {path}: {e}") from e
    except ValueError as e:
        raise DsnLayersError(f"the layer policy {path} is not JSON: {e}") from e
    if isinstance(data, Mapping) and isinstance(data.get("dsn"), Mapping):
        return dict(data["dsn"])
    if isinstance(data, Mapping) and "signal_layers" in data:
        return dict(data)
    raise DsnLayersError(f"{path} is neither a physical-design artifact with a 'dsn' section "
                         "nor a layer policy (no signal_layers)")


# --- reading the DSN ---------------------------------------------------------
@dataclass(frozen=True)
class _Board:
    text: str
    quote: str
    root: ds.Node
    layers: tuple[str, ...]                  # structure order
    layer_tokens: Mapping[str, str]          # layer -> its token as written
    types: Mapping[str, ds.Token]            # layer -> its (type X) token
    planes: tuple[tuple[str, str], ...]      # (net, layer) of every (plane ...)
    pins: Mapping[str, tuple[str, ...]]      # net -> its pin reference tokens
    classes: tuple[ds.Node, ...]
    unit: str


def _tokens(node: ds.Node) -> list[ds.Token]:
    return [x for x in node.items if isinstance(x, ds.Token)]


def _first_node(node: ds.Node) -> ds.Node | None:
    return next((x for x in node.items if isinstance(x, ds.Node)), None)


def _layers(structure: ds.Node, q: str) -> tuple[tuple[str, ...], dict, dict]:
    names, tokens, types = [], {}, {}
    for lay in ds.find_all(structure, "layer"):
        toks = _tokens(lay)
        if not toks:
            continue
        name = ds.unquote(toks[0].value, q)
        names.append(name)
        tokens[name] = toks[0].value
        kind = ds.child(lay, "type")
        if kind is not None and _tokens(kind):
            types[name] = _tokens(kind)[0]
    return tuple(names), tokens, types


def _planes(structure: ds.Node, q: str) -> tuple[tuple[str, str], ...]:
    out = []
    for plane in ds.find_all(structure, "plane"):
        toks, shape = _tokens(plane), _first_node(plane)
        if toks and shape is not None and _tokens(shape):
            out.append((ds.unquote(toks[0].value, q), ds.unquote(_tokens(shape)[0].value, q)))
    return tuple(out)


def _pins(network: ds.Node, q: str) -> dict[str, tuple[str, ...]]:
    out = {}
    for net in ds.find_all(network, "net"):
        toks, pins = _tokens(net), ds.child(net, "pins")
        if toks and pins is not None:
            out[ds.unquote(toks[0].value, q)] = tuple(t.value for t in _tokens(pins))
    return out


def _unit(root: ds.Node) -> str:
    for name in ("unit", "resolution"):
        node = ds.child(root, name)
        if node is not None and _tokens(node):
            return _tokens(node)[0].value.lower()
    return "um"


def _read_board(text: str) -> _Board:
    try:
        roots, q = ds.parse_nodes(text), ds.declared_quote(text)
    except ds.DsnError as e:
        raise DsnLayersError(f"the DSN does not parse: DsnError: {e}") from e
    if len(roots) != 1 or roots[0].name != "pcb":
        raise DsnLayersError("not a Specctra design: expected one (pcb ...) list")
    root = roots[0]
    structure, network = ds.child(root, "structure"), ds.child(root, "network")
    if structure is None:
        raise DsnLayersError("the DSN has no (structure ...) section")
    if network is None:
        raise DsnLayersError("the DSN has no (network ...) section")
    layers, tokens, types = _layers(structure, q)
    if not layers:
        raise DsnLayersError("the DSN structure has no (layer ...)")
    return _Board(text, q, root, layers, tokens, types, _planes(structure, q),
                  _pins(network, q), tuple(ds.find_all(network, "class")), _unit(root))


def _ordered(board: _Board, names: Iterable[str]) -> tuple[str, ...]:
    wanted = set(names)
    return tuple(layer for layer in board.layers if layer in wanted)


# --- SMD pad layers (the pad-layer rule) -------------------------------------
def split_pin_ref(token: str, quote: str = ds.DEFAULT_QUOTE) -> tuple[str, str]:
    """(reference, pad id) of a DSN pin reference written [q]comp[q]-[q]pin[q]
    (U1-6, "R 1"-2, U1-"A-1"); KiCad quotes a reference holding '-'. An
    unterminated quoted part raises DsnLayersError."""
    try:
        if token.startswith(quote):
            close = token.find(quote, 1)
            if close < 0:
                raise ds.DsnError("unterminated quoted reference", 0, token)
            rest = token[close + 1:]
            return token[1:close], ds.unquote(rest[1:], quote) if rest.startswith("-") else ""
        dash = token.find("-", 1)
        if dash < 0:
            return token, ""
        return token[:dash], ds.unquote(token[dash + 1:], quote)
    except ds.DsnError as e:
        raise DsnLayersError(f"pin reference {token!r} does not parse: {e}") from e


def _places(board: _Board) -> dict[str, tuple[str, bool]]:
    """reference -> (image, placed on the back)."""
    placement, q, out = ds.child(board.root, "placement"), board.quote, {}
    for comp in ds.find_all(placement, "component") if placement else []:
        image = ds.unquote(_tokens(comp)[0].value, q) if _tokens(comp) else ""
        for place in ds.find_all(comp, "place"):
            toks = _tokens(place)
            if toks:
                back = len(toks) > 3 and toks[3].value == "back"
                out[ds.unquote(toks[0].value, q)] = (image, back)
    return out


def _library(board: _Board) -> tuple[dict[str, dict[str, str]], dict[str, tuple[str, ...]]]:
    """({image: {pad id: padstack}}, {padstack: its copper layers})."""
    library, q = ds.child(board.root, "library"), board.quote
    images, stacks = {}, {}
    for image in ds.find_all(library, "image") if library else []:
        pins = (_tokens(p) for p in ds.find_all(image, "pin"))
        images[ds.unquote(_tokens(image)[0].value, q)] = {
            ds.unquote(t[1].value, q): ds.unquote(t[0].value, q) for t in pins if len(t) > 1}
    for stack in ds.find_all(library, "padstack") if library else []:
        shapes = (_first_node(s) for s in ds.find_all(stack, "shape"))
        layers = [ds.unquote(_tokens(g)[0].value, q) for g in shapes if g and _tokens(g)]
        stacks[ds.unquote(_tokens(stack)[0].value, q)] = tuple(dict.fromkeys(layers))
    return images, stacks


def _pad_layer(board: _Board, stack_layers: tuple[str, ...], back: bool) -> str | None:
    """The board layer of a single-layer (SMD) padstack; None for through
    holes. A back-side part mirrors the layer, as Freerouting places it."""
    if len(stack_layers) != 1 or stack_layers[0] not in board.layers:
        return None
    k = board.layers.index(stack_layers[0])
    return board.layers[len(board.layers) - 1 - k] if back else stack_layers[0]


def _smd_pads(board: _Board) -> dict[str, tuple[str, ...]]:
    places, (images, stacks) = _places(board), _library(board)
    out = {}
    for net, pins in board.pins.items():
        layers = set()
        for token in pins:
            ref, pad = split_pin_ref(token, board.quote)
            image, back = places.get(ref, ("", False))
            stack = images.get(image, {}).get(pad, "")
            layer = _pad_layer(board, stacks.get(stack, ()), back)
            layers |= {layer} if layer else set()
        if layers:
            out[net] = _ordered(board, layers)
    return out


def smd_pad_layers(dsn_text: str) -> dict[str, tuple[str, ...]]:
    """{net: the layers of its SMD pads, in DSN layer order} (through-hole
    pads skipped; back-side parts mirrored)."""
    return _smd_pads(_read_board(dsn_text))


# --- where each net may route ------------------------------------------------
@dataclass(frozen=True)
class _Route:
    kind: str                                # base | plane | net
    layers: tuple[str, ...] | None           # None: no use_layer (every layer)
    width_mm: float | None = None
    layer_rules: tuple[tuple[str, float], ...] = ()
    excluded: tuple[str, ...] = ()


def _check_fit(board: _Board, pol: Policy) -> None:
    planes = {layer for layers in pol.plane_nets.values() for layer in layers}
    named = {*pol.signal_layers, *pol.unused_layers, *planes,
             *(x for nl in pol.net_layers.values() for x in (nl.primary, *nl.widths))}
    unknown = sorted(x for x in named if x not in board.layers)
    if unknown:
        raise DsnLayersError(f"the policy names layers the DSN does not have: {unknown} (DSN "
                             f"layers {list(board.layers)}): a stale artifact?")
    for a, b, what in ((pol.signal_layers, pol.unused_layers, "signal and unused"),
                       (planes, pol.unused_layers, "plane and unused")):
        both = sorted(set(a) & set(b))
        if both:
            raise DsnLayersError(f"layers {both} are both {what}")
    unsaid = [x for x in board.layers if x not in {*pol.signal_layers, *planes,
                                                   *pol.unused_layers}]
    if unsaid:
        raise DsnLayersError(f"the policy does not say what {unsaid} is (signal, plane or "
                             "unused)")
    _check_nets(pol)


def _check_nets(pol: Policy) -> None:
    both = sorted(set(pol.plane_nets) & set(pol.net_layers))
    if both:
        raise DsnLayersError(f"nets {both} are both plane nets and impedance nets")
    routable = set(pol.signal_layers) - set(pol.unused_layers)
    for net, nl in pol.net_layers.items():
        if nl.primary not in routable:
            raise DsnLayersError(f"net {net}: primary layer {nl.primary} is not a routable "
                                 "(signal or mixed) layer")


def narrow(board_layers: Sequence[str], routable: Sequence[str], nl: NetLayers,
           verified: bool) -> tuple[tuple[str, ...], tuple[tuple[str, float], ...],
                                    tuple[str, ...]]:
    """(Freerouting layers, layer rules, excluded allowed layers) of one
    impedance net (ADR D7.9): the primary layer plus every routable layer
    whose solved width lies in the primary's window; the other routable
    allowed layers join with their own widths only when ``verified``."""
    lo, hi = nl.window
    eligible = [x for x in routable if x in nl.widths]
    inside = {x for x in eligible
              if x == nl.primary or lo - _EPS_MM <= nl.widths[x] <= hi + _EPS_MM}
    outside = [x for x in eligible if x not in inside]
    extra = set(outside) if verified else set()
    routed = tuple(x for x in board_layers if x in inside or x in extra)
    rules = tuple((x, nl.widths[x]) for x in board_layers if x in extra)
    excluded = tuple(x for x in board_layers if x in nl.widths and x not in routed)
    return routed, rules, excluded


def _routes(board: _Board, pol: Policy, verified: bool) -> tuple[dict[str, _Route], _Route]:
    base = _ordered(board, set(pol.signal_layers) - set(pol.unused_layers))
    routes = {}
    for net, layers in pol.plane_nets.items():
        if pol.plane_classes == UNRESTRICTED:
            every = _ordered(board, set(board.layers) - set(pol.unused_layers))
            routes[net] = _Route("plane", None if every == board.layers else every)
        else:
            routes[net] = _Route("plane", _ordered(board, {*base, *layers}))
    for net, nl in pol.net_layers.items():
        routed, rules, excluded = narrow(board.layers, base, nl, verified)
        routes[net] = _Route("net", routed, nl.widths[nl.primary], rules, excluded)
    return routes, _Route("base", base)


# --- splitting the classes ---------------------------------------------------
@dataclass(frozen=True)
class _Plan:
    name: str                                # unquoted
    token: str                               # as written
    source: str                              # the KiCad class it came from
    members: tuple[ds.Token, ...]
    route: _Route
    keep_members: bool                       # the member list is left as written


def _class_head(node: ds.Node) -> tuple[ds.Token | None, tuple[ds.Token, ...]]:
    """(the class-name token, the member tokens before the first child list)."""
    head = []
    for x in node.items:
        if isinstance(x, ds.Node):
            break
        head.append(x)
    return (head[0] if head else None), tuple(head[1:])


def _refuse_processed(board: _Board) -> None:
    for node in board.classes:
        name, _members = _class_head(node)
        circuit = ds.child(node, "circuit")
        done = name is not None and name.value.startswith(("kicad_eda_", '"kicad_eda_'))
        if done or (circuit is not None and ds.child(circuit, "use_layer") is not None):
            raise DsnLayersError(f"the DSN already carries a layer policy (class "
                                 f"{name.value if name else '?'}): post-process KiCad's raw "
                                 "export once")


def _unique(base: str, taken: frozenset[str]) -> str:
    name, k = base, 2
    while name in taken:
        name, k = f"{base}_{k}", k + 1
    return name


def _split_class(board: _Board, node: ds.Node, routes: Mapping[str, _Route], base: _Route,
                 taken: frozenset[str]) -> tuple[list[_Plan], frozenset[str]]:
    """The classes one KiCad class becomes (the members that stay first, then
    one class per plane / impedance member), and the class names now taken."""
    name_tok, members = _class_head(node)
    src = ds.unquote(name_tok.value, board.quote)
    stay = tuple(m for m in members if ds.unquote(m.value, board.quote) not in routes)
    keep = len(stay) == len(members)
    plans = [_Plan(src, name_tok.value, src, stay, base, keep)] if stay else []
    for m in members:
        net = ds.unquote(m.value, board.quote)
        route = routes.get(net)
        if route is not None:
            prefix = PLANE_CLASS_PREFIX if route.kind == "plane" else NET_CLASS_PREFIX
            safe = "".join(c if c in _CLASS_SAFE else "_" for c in net) or "_"
            cname = _unique(prefix + safe, taken)
            taken = taken | {cname}
            plans.append(_Plan(cname, cname, src, (m,), route, False))
    return plans, taken


def _plans(board: _Board, routes: Mapping[str, _Route],
           base: _Route) -> list[tuple[ds.Node, list[_Plan]]]:
    taken = frozenset(ds.unquote(n.value, board.quote) for n in
                      (_class_head(c)[0] for c in board.classes) if n is not None)
    out, seen = [], {}
    for node in board.classes:
        name_tok, members = _class_head(node)
        if name_tok is None or not members:
            continue
        for m in members:
            net = ds.unquote(m.value, board.quote)
            if net in seen:
                raise DsnLayersError(f"net {net} is in two classes: {seen[net]} and "
                                     f"{name_tok.value}")
            seen[net] = name_tok.value
        split, taken = _split_class(board, node, routes, base, taken)
        out.append((node, split))
    loose = sorted(n for n in board.pins if n not in seen)
    if loose:
        raise DsnLayersError(f"nets {loose} are in no class: the layer policy cannot restrict "
                             "them")
    return out


# --- writing the classes (span edits on KiCad's own text) --------------------
def _fmt(value: float) -> str:
    text = f"{round(value, 4):.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _column(text: str, offset: int) -> int:
    return offset - (text.rfind("\n", 0, offset) + 1)


def _indent(text: str, offset: int) -> str:
    lead = text[text.rfind("\n", 0, offset) + 1:offset]
    return lead if not lead.strip() else ""


def _wrap(tokens: Sequence[str], first_col: int, indent: str, nl: str) -> str:
    lines, line, col = [], [], first_col
    for tok in tokens:
        add = len(tok) + (1 if line else 0)
        if line and col + add > WRAP_COLUMNS:
            lines.append(" ".join(line))
            line, col = [tok], len(indent) + len(tok)
        else:
            line, col = [*line, tok], col + add
    return (nl + indent).join([*lines, " ".join(line)])


def _head_end(text: str, node: ds.Node) -> int:
    """The offset just after a list's head atom."""
    stop = node.items[0].start if node.items else node.end
    return node.start + ds.tokenize(text[node.start:stop])[1].end


def _before(text: str, node: ds.Node, item: ds.Node | ds.Token) -> str:
    """The whitespace KiCad wrote before ``item``, a direct child of ``node``."""
    k = node.items.index(item)
    prev_end = node.items[k - 1].end if k else _head_end(text, node)
    return text[prev_end:item.start]


def _insert_first(text: str, node: ds.Node, new: str) -> list[tuple[int, int, str]]:
    """``new`` as the first item of ``node``, laid out like its items."""
    if node.items:
        first = node.items[0]
        return [(first.start, first.start, new + _before(text, node, first))]
    return [(node.end - 1, node.end - 1, " " + new)]


def _use_layer_edits(board: _Board, node: ds.Node,
                     layers: tuple[str, ...] | None) -> list[tuple[int, int, str]]:
    if layers is None:
        return []
    use = "(use_layer " + " ".join(board.layer_tokens[x] for x in layers) + ")"
    circuit = ds.child(node, "circuit")
    if circuit is not None:
        return _insert_first(board.text, circuit, use)
    child = _first_node(node)
    if child is not None:
        sep = _before(board.text, node, child)
        return [(child.start, child.start, f"(circuit {use})" + sep)]
    return [(node.end - 1, node.end - 1, f" (circuit {use})")]


def _per_mm(board: _Board) -> float:
    if board.unit not in _UNITS_PER_MM:
        raise DsnLayersError(f"the DSN unit {board.unit!r} is not one of "
                             f"{sorted(_UNITS_PER_MM)}: cannot write widths")
    return _UNITS_PER_MM[board.unit]


def _width_edits(board: _Board, node: ds.Node, width_mm: float) -> list[tuple[int, int, str]]:
    value = _fmt(width_mm * _per_mm(board))
    rule = ds.child(node, "rule")
    width = ds.child(rule, "width") if rule is not None else None
    if width is not None and _tokens(width):
        tok = _tokens(width)[0]
        return [(tok.start, tok.end, value)]
    if rule is not None:
        return _insert_first(board.text, rule, f"(width {value})")
    return [(node.end - 1, node.end - 1, f" (rule (width {value}))")]


def _layer_rule_edits(board: _Board, node: ds.Node,
                      rules: tuple[tuple[str, float], ...]) -> list[tuple[int, int, str]]:
    per_mm = _per_mm(board)
    texts = [f"(layer_rule {board.layer_tokens[x]} (rule (width {_fmt(w * per_mm)})))"
             for x, w in rules]
    rule = ds.child(node, "rule")
    if rule is not None:
        sep = _before(board.text, node, rule)
        return [(rule.end, rule.end, "".join(sep + t for t in texts))]
    return [(node.end - 1, node.end - 1, "".join(" " + t for t in texts))]


def _render(board: _Board, node: ds.Node, plan: _Plan, nl: str) -> str:
    text = board.text
    name_tok, members = _class_head(node)
    edits = [] if plan.token == name_tok.value else [(name_tok.start, name_tok.end, plan.token)]
    if not plan.keep_members:
        col = _column(text, name_tok.start) + len(plan.token) + 1
        wrapped = _wrap([m.value for m in plan.members], col, _indent(text, node.start) + "  ", nl)
        edits.append((members[0].start, members[-1].end, wrapped))
    edits += _use_layer_edits(board, node, plan.route.layers)
    if plan.route.width_mm is not None:
        edits += _width_edits(board, node, plan.route.width_mm)
    if plan.route.layer_rules:
        edits += _layer_rule_edits(board, node, plan.route.layer_rules)
    return ds.replace_spans(text[node.start:node.end],
                            [(s - node.start, e - node.start, new) for s, e, new in edits])


def _edits(board: _Board, plans: list[tuple[ds.Node, list[_Plan]]],
           nl: str) -> list[tuple[int, int, str]]:
    edits = [(tok.start, tok.end, "signal") for tok in board.types.values()
             if tok.value == "power"]
    for node, split in plans:
        joiner = nl + _indent(board.text, node.start)
        edits.append((node.start, node.end, joiner.join(_render(board, node, p, nl)
                                                        for p in split)))
    return edits


# --- the report --------------------------------------------------------------
def _class_row(board: _Board, plan: _Plan) -> dict[str, Any]:
    route = plan.route
    return {"name": plan.name, "from": plan.source,
            "nets": [ds.unquote(m.value, board.quote) for m in plan.members],
            "use_layer": list(route.layers) if route.layers is not None else None,
            "width_mm": route.width_mm, "layer_rules": dict(route.layer_rules)}


def _warnings(board: _Board, pol: Policy) -> list[str]:
    out = [f"policy key(s) {list(pol.ignored_keys)} are not part of the layer policy and were "
           "ignored"] if pol.ignored_keys else []
    for kind, nets in (("plane net", pol.plane_nets), ("impedance net", pol.net_layers)):
        out += [f"{kind} {net} is not in the DSN network (no pads): nothing to restrict"
                for net in nets if net not in board.pins]
    owners = {x: net for net, layers in pol.plane_nets.items() for x in layers}
    for net, layer in board.planes:
        if layer in pol.unused_layers:
            out.append(f"the DSN pours {net} on unused layer {layer}")
        elif layer in owners and owners[layer] != net:
            out.append(f"the DSN pours {net} on plane layer {layer}, which the policy gives "
                       f"to {owners[layer]}")
    return out


def _pads_outside(board: _Board, routes: Mapping[str, _Route],
                  base: _Route) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """(impedance nets, other nets) of two or more pins with an SMD pad layer
    outside their routed layers: pad_layer_excluded and pad_layer_blocked."""
    excluded, blocked = {}, {}
    for net, pads in _smd_pads(board).items():
        route = routes.get(net, base)
        missing = [x for x in pads if route.layers is not None and x not in route.layers]
        if missing and len(board.pins.get(net, ())) > 1:
            (excluded if route.kind == "net" else blocked)[net] = missing
    return excluded, blocked


def _report(board: _Board, pol: Policy, routes: Mapping[str, _Route], base: _Route,
            plans: list[tuple[ds.Node, list[_Plan]]], verified: bool) -> dict[str, Any]:
    flat = [p for _node, split in plans for p in split]
    moved = {ds.unquote(p.members[0].value, board.quote): p.name
             for p in flat if p.route.kind != "base"}
    nets = {n: r for n, r in routes.items() if r.kind == "net" and n in board.pins}
    excluded, blocked = _pads_outside(board, routes, base)
    stuck = [f"net {n}: SMD pads on {', '.join(x)} lie outside its routed layers; Freerouting "
             "may leave them unrouted" for n, x in blocked.items()]
    return {"variant": pol.plane_classes, "source": pol.source,
            "retyped": [x for x, tok in board.types.items() if tok.value == "power"],
            "classes": [_class_row(board, p) for p in flat],
            "nets_moved": dict(sorted(moved.items())),
            "routed_layers": {n: list(r.layers or ()) for n, r in sorted(nets.items())},
            "layer_rules": {n: dict(r.layer_rules) for n, r in sorted(nets.items())
                            if r.layer_rules},
            "excluded_layers": {n: list(r.excluded) for n, r in sorted(nets.items())
                                if r.excluded},
            "pad_layer_excluded": excluded, "pad_layer_blocked": blocked,
            "unused_layers": list(pol.unused_layers), "layer_rule_verified": verified,
            "layer_rule_evidence": LAYER_RULE_EVIDENCE,
            "warnings": [*_warnings(board, pol), *stuck]}


# --- entry points ------------------------------------------------------------
def postprocess(dsn_text: str, policy: Mapping | Policy, *, variant: str | None = None,
                layer_rule_verified: bool | None = None) -> tuple[str, dict[str, Any]]:
    """(the DSN text with the layer policy applied, the report); see the
    module doc. ``variant`` overrides the policy's plane_classes;
    ``layer_rule_verified`` overrides LAYER_RULE_VERIFIED (tests and the
    one-time check). Raises DsnLayersError; never mutates its inputs."""
    pol = load_policy(policy)
    if variant is not None:
        if variant not in VARIANTS:
            raise DsnLayersError(f"variant must be one of {list(VARIANTS)}, got {variant!r}")
        pol = dataclasses.replace(pol, plane_classes=variant)
    verified = LAYER_RULE_VERIFIED if layer_rule_verified is None else bool(layer_rule_verified)
    board = _read_board(dsn_text)
    _refuse_processed(board)
    _check_fit(board, pol)
    routes, base = _routes(board, pol, verified)
    plans = _plans(board, routes, base)
    nl = "\r\n" if "\r\n" in dsn_text else "\n"
    text = ds.replace_spans(dsn_text, _edits(board, plans, nl))
    return text, _report(board, pol, routes, base, plans, verified)


def _write_atomic(path: Path, text: str) -> None:
    handle, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as f:
            f.write(text.encode("utf-8"))
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise DsnLayersError(f"cannot write {path}: {e}") from e


def postprocess_file(path: str | os.PathLike, policy: Mapping | Policy,
                     **kwargs: Any) -> dict[str, Any]:
    """postprocess the DSN file at ``path`` in place (bytes as written, newlines
    kept, atomic replace); the report."""
    dsn = Path(path)
    try:
        raw = dsn.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise DsnLayersError(f"cannot read {dsn}: {type(e).__name__}: {e}") from e
    text, report = postprocess(raw, policy, **kwargs)
    if text != raw:
        _write_atomic(dsn, text)
    return report


def derive_policy_from_dsn(dsn_text: str) -> dict[str, Any]:
    """A layer policy from the DSN alone (no artifact): plane layers are the
    ``(type power)`` layers and the inner layers that carry a (plane ...);
    an outer pour is never a plane (E10); a power layer with no pour is
    unused. ``notes`` says what was ignored."""
    board = _read_board(dsn_text)
    outer = {board.layers[0], board.layers[-1]}
    poured: dict[str, list[str]] = {}
    for net, layer in board.planes:
        nets = poured.get(layer, [])
        poured[layer] = nets if net in nets else [*nets, net]
    power = {x for x, tok in board.types.items() if tok.value == "power"}
    planes = [x for x in board.layers if x in power or (x not in outer and x in poured)]
    plane_nets: dict[str, list[str]] = {}
    for layer in planes:
        for net in poured.get(layer, []):
            plane_nets[net] = [*plane_nets.get(net, []), layer]
    unused = [x for x in planes if x not in poured]
    notes = [f"{x} is typed power but has no (plane ...) pour: unused" for x in unused]
    notes += [f"the {net} pour on outer layer {layer} is not a plane (E10)"
              for net, layer in board.planes if layer in outer and layer not in planes]
    return {"signal_layers": [x for x in board.layers if x not in planes],
            "plane_nets": plane_nets, "unused_layers": unused, "net_layers": {},
            "source": "derived", "notes": notes}
