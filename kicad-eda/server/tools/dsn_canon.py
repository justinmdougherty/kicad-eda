#!/usr/bin/env python3
"""Canonical Specctra DSN order before Freerouting (F5-02).

Why: KiCad saves footprints, vias and zones in random-UUID order, the DSN
export follows that order, and Freerouting 1.9.0 is deterministic for one DSN
but order-sensitive: 5 of 20 standalone buck_5v3a runs failed G4 on the same
placement (T07). Rewriting the DSN in an order that depends on the design
only made 9 different raw DSNs one hash and the SES identical every time.

canonicalize(text, seed=0, name=None) -> str (the DSN text, '\\n' line ends,
one trailing newline). seed 0 is the canonical order:

- placement: components by image name (natural sort), the places inside a
  component by reference (natural); other placement entries first, as written;
- library: images by name, padstacks (and anything else) after the images in
  the order KiCad wrote them (KiCad already writes them sorted);
- network: nets by name, each (pins ...) list in natural order (U1-3 before
  U1-10); classes untouched;
- wiring: entries sorted by their emitted text;
- structure: each run of consecutive zone entries (plane / keepout kinds)
  sorted by emitted text (KiCad writes zones on the same layers in UUID
  order); every other structure entry keeps its place;
- the (pcb <id>) id, which KiCad writes as the absolute DSN path, becomes
  ``name`` (default: the basename of that path), so runs in different
  directories give identical bytes.

seed k > 0 is a deterministic alternate order for router retries: the
footprint (reference) order random.Random(k).shuffle(<references in natural
order>) applied the way KiCad's footprint iteration would write it (component
groups by first reference, places in footprint order, library images by first
use, every net's pins by footprint index then the pad's rank in its image,
nets by their first pin). It starts from the canonical form, so it depends on
the design and k only. footprint_order(text, seed) returns that order.

canonicalize_file(path, seed=0, name=None) rewrites the file in place
(default name: the file's basename) and returns the sha256 of the bytes
written. Sort keys compare names unquoted with natural_key; ties fall back to
the emitted text, so the order is total. Whitespace and the layout follow
dsn_sexpr.emit; the token multiset is preserved except the id.

Malformed text, a top-level list that is not (pcb ...), or a file that is not
UTF-8 raises DsnError (a ValueError); a bad seed raises TypeError/ValueError.
Stdlib only and Python 3.11 compatible (KiCad 9's bundled Python imports it);
all DSN reading goes through dsn_sexpr, never regular expressions.
"""
from __future__ import annotations

import hashlib
import itertools
import os
import random
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

import dsn_sexpr as ds
from dsn_sexpr import DsnError

__all__ = ["DsnError", "canonicalize", "canonicalize_file", "footprint_order", "natural_key"]

_DIGITS = frozenset("0123456789")
_ZONES = frozenset({"plane", "keepout", "via_keepout", "wire_keepout", "bend_keepout",
                    "elongate_keepout", "place_keepout"})
_UNSEEN = 1 << 30                       # sorts after every real footprint index / pad rank

Key = Callable[[object], tuple]


# ----------------------------------------------------------------------------
# keys
# ----------------------------------------------------------------------------
def natural_key(name: str) -> tuple:
    """Natural sort key: runs of ASCII digits compare as numbers (U1-3 before
    U1-10, R9 before R10), a digit run before a text run; the key keeps every
    character, so different names never tie."""
    return tuple((0, int(run), run) if digits else (1, run, "")
                 for digits, run in ((d, "".join(g)) for d, g in
                                     itertools.groupby(name, key=lambda c: c in _DIGITS)))


def _head(item: object) -> str | None:
    if isinstance(item, list) and item and isinstance(item[0], str):
        return item[0]
    return None


def _name(item: object, quote: str) -> str:
    """The unquoted name of a list headed (kind <name> ...), else ''."""
    if isinstance(item, list) and len(item) > 1 and isinstance(item[1], str):
        return ds.unquote(item[1], quote)
    return ""


def _atom_key(quote: str) -> Key:
    """An atom by its natural unquoted name, a list by its emitted text."""
    def key(item: object) -> tuple:
        if isinstance(item, str):
            return (0, natural_key(ds.unquote(item, quote)), item)
        return (1, (), ds.emit(item))
    return key


def _named_key(quote: str) -> Key:
    """A list headed (kind <name> ...) by its natural unquoted name."""
    def key(item: object) -> tuple:
        name = item[1] if isinstance(item, list) and len(item) > 1 else None
        if not isinstance(name, str):
            return ((), ds.emit(item))
        return (natural_key(ds.unquote(name, quote)), ds.emit(item))
    return key


def _split(items: Iterable, kind: str) -> tuple[list, list]:
    """(the lists headed `kind`, everything else), each in order."""
    items = list(items)
    return [x for x in items if _head(x) == kind], [x for x in items if _head(x) != kind]


# ----------------------------------------------------------------------------
# seed 0: the canonical order
# ----------------------------------------------------------------------------
def _canon_component(comp: list, quote: str) -> list:
    if len(comp) < 2 or not isinstance(comp[1], str):
        return list(comp)
    places, rest = _split(comp[2:], "place")
    return [*comp[:2], *rest, *sorted(places, key=_named_key(quote))]


def _canon_placement(section: list, quote: str) -> list:
    comps, other = _split(section[1:], "component")
    ordered = sorted((_canon_component(c, quote) for c in comps), key=_named_key(quote))
    return [section[0], *other, *ordered]


def _canon_library(section: list, quote: str) -> list:
    images, rest = _split(section[1:], "image")
    return [section[0], *sorted(images, key=_named_key(quote)), *rest]


def _canon_net(net: list, quote: str) -> list:
    return [[x[0], *sorted(x[1:], key=_atom_key(quote))] if _head(x) == "pins" else x
            for x in net]


def _canon_network(section: list, quote: str) -> list:
    nets, others = _split(section[1:], "net")
    ordered = sorted((_canon_net(n, quote) for n in nets), key=_named_key(quote))
    return [section[0], *ordered, *others]


def _canon_wiring(section: list, quote: str) -> list:
    return [section[0], *sorted(section[1:], key=ds.emit)]


def _canon_structure(section: list, quote: str) -> list:
    out: list = [section[0]]
    for is_zone, group in itertools.groupby(section[1:], key=lambda x: _head(x) in _ZONES):
        run = list(group)
        out.extend(sorted(run, key=ds.emit) if is_zone else run)
    return out


_SECTIONS: dict[str, Callable[[list, str], list]] = {
    "structure": _canon_structure, "placement": _canon_placement, "library": _canon_library,
    "network": _canon_network, "wiring": _canon_wiring}


def _canonical(tree: list, quote: str) -> list:
    return [_SECTIONS[_head(x)](x, quote) if _head(x) in _SECTIONS else x for x in tree]


# ----------------------------------------------------------------------------
# seed k > 0: a deterministic alternate footprint order
# ----------------------------------------------------------------------------
def _section(tree: list, name: str) -> list:
    return ds.find(tree, name) or [name]


def _place_refs(tree: list, quote: str) -> list[str]:
    return [_name(p, quote) for comp in ds.children(_section(tree, "placement"), "component")
            for p in ds.children(comp, "place") if _name(p, quote)]


def _footprint_order(tree: list, quote: str, seed: int) -> list[str]:
    order = sorted(set(_place_refs(tree, quote)), key=natural_key)
    if seed:
        random.Random(seed).shuffle(order)
    return order


def _split_pin(token: str, quote: str) -> tuple[str, str]:
    """(reference, pad) of a pin reference: comp-pin, "R 1"-2, U1-"A-1"."""
    if token.startswith(quote):
        close = token.find(quote, 1)
        if close > 0 and token[close + 1:close + 2] == "-":
            return token[1:close], ds.unquote(token[close + 2:], quote)
        return ds.unquote(token, quote), ""
    dash = token.find("-", 1)
    if dash < 0:
        return token, ""
    return token[:dash], ds.unquote(token[dash + 1:], quote)


def _pin_ranks(library: list, quote: str) -> dict[str, dict[str, int]]:
    """image name -> {pad id: its rank in the image} (KiCad walks a
    footprint's pads in that order)."""
    ranks: dict[str, dict[str, int]] = {}
    for image in ds.children(library, "image"):
        ids = [next((x for x in pin[2:] if isinstance(x, str)), "")
               for pin in ds.children(image, "pin")]
        ranks.setdefault(_name(image, quote), {ds.unquote(p, quote): k
                                               for k, p in reversed(list(enumerate(ids)))})
    return ranks


class _Model:
    """Where a footprint order puts each reference, image and pin."""

    def __init__(self, tree: list, quote: str, order: Sequence[str]) -> None:
        self.quote = quote
        self.index = {ref: k for k, ref in enumerate(order)}
        comps = ds.children(_section(tree, "placement"), "component")
        self.ref_image = {_name(p, quote): _name(c, quote)
                          for c in comps for p in ds.children(c, "place")}
        self.ranks = _pin_ranks(_section(tree, "library"), quote)

    def ref(self, place: list) -> int:
        return self.index.get(_name(place, self.quote), _UNSEEN)

    def pin(self, token: object) -> tuple[int, int]:
        if not isinstance(token, str):
            return (_UNSEEN, _UNSEEN)
        ref, pad = _split_pin(token, self.quote)
        rank = self.ranks.get(self.ref_image.get(ref, ""), {}).get(pad, _UNSEEN)
        return (self.index.get(ref, _UNSEEN), rank)

    def component(self, comp: list) -> list:
        if not _name(comp, self.quote):
            return list(comp)
        places, rest = _split(comp[2:], "place")
        return [*comp[:2], *rest, *sorted(places, key=self.ref)]

    def net(self, net: list) -> list:
        return [[x[0], *sorted(x[1:], key=self.pin)] if _head(x) == "pins" else x for x in net]

    def first_pin(self, net: list) -> tuple[int, int]:
        pins = ds.find(net, "pins")
        return self.pin(pins[1]) if pins and len(pins) > 1 else (_UNSEEN, _UNSEEN)


def _permute_placement(section: list, model: _Model) -> list:
    comps, other = _split(section[1:], "component")
    placed = [model.component(c) for c in comps]
    first = [min(map(model.ref, ds.children(c, "place")), default=_UNSEEN) for c in placed]
    ordered = [c for _, _, c in sorted(zip(first, itertools.count(), placed))]
    return [section[0], *other, *ordered]


def _permute_library(section: list, model: _Model, used: dict[str, int]) -> list:
    images, rest = _split(section[1:], "image")
    ordered = sorted(images, key=lambda im: used.get(_name(im, model.quote), _UNSEEN))
    return [section[0], *ordered, *rest]


def _permute_network(section: list, model: _Model) -> list:
    nets, others = _split(section[1:], "net")
    ordered = sorted((model.net(n) for n in nets), key=model.first_pin)
    return [section[0], *ordered, *others]


def _permuted(tree: list, quote: str, order: Sequence[str]) -> list:
    """The canonical `tree` rewritten in the footprint `order` (stable sorts:
    whatever the order does not decide stays canonical)."""
    model = _Model(tree, quote, order)
    placement = _permute_placement(_section(tree, "placement"), model)
    used = {_name(c, quote): k
            for k, c in reversed(list(enumerate(ds.children(placement, "component"))))}
    rebuilt = {"placement": placement,
               "library": _permute_library(_section(tree, "library"), model, used),
               "network": _permute_network(_section(tree, "network"), model)}
    return [rebuilt[_head(x)] if _head(x) in rebuilt else x for x in tree]


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------
def _check_seed(seed: object) -> None:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError(f"seed must be an int >= 0, got {seed!r}")
    if seed < 0:
        raise ValueError(f"seed must be >= 0 (0 = the canonical order), got {seed}")


def _read_pcb(text: str) -> tuple[list, str]:
    """(the parsed (pcb ...) tree, the declared quote character)."""
    tree = ds.parse(text)
    if _head(tree) != "pcb":
        shown = tree[0] if tree and isinstance(tree[0], str) else "a list"
        raise DsnError(f"not a Specctra DSN: the top-level list is ({shown} ...), "
                       "not (pcb ...)", 0)
    twice = [name for name in _SECTIONS if len(ds.children(tree, name)) > 1]
    if twice:
        raise DsnError(f"more than one ({twice[0]} ...) section", 0)
    return tree, ds.declared_quote(text)


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1] or path


def _renamed(tree: list, quote: str, name: str | None) -> list:
    has_id = len(tree) > 1 and isinstance(tree[1], str)
    if name is None:
        if not has_id:
            return tree
        name = _basename(ds.unquote(tree[1], quote))
    clean = name.replace(quote, "_").replace("\r", "_").replace("\n", "_")
    return [tree[0], ds.quote(clean, quote), *tree[2 if has_id else 1:]]


def footprint_order(text: str, seed: int) -> list[str]:
    """The footprint (reference) order seed `seed` applies: the references in
    natural order for seed 0, random.Random(seed).shuffle of that list for
    seed > 0. Names are unquoted."""
    _check_seed(seed)
    tree, quote = _read_pcb(text)
    return _footprint_order(tree, quote, seed)


def canonicalize(text: str, seed: int = 0, name: str | None = None) -> str:
    """`text` (a KiCad Specctra DSN) in the canonical order (seed 0) or the
    seed's alternate footprint order, with the (pcb <id>) id set to `name`
    (default: the basename of the id). See the module doc."""
    _check_seed(seed)
    tree, quote = _read_pcb(text)
    canon = _canonical(tree, quote)
    if seed:
        canon = _permuted(canon, quote, _footprint_order(canon, quote, seed))
    return ds.emit(_renamed(canon, quote, name)) + "\n"


def canonicalize_file(path: str | os.PathLike, seed: int = 0, name: str | None = None) -> str:
    """Rewrite the DSN at `path` in place with canonicalize (default name: the
    file's basename) and return the sha256 hex digest of the bytes written.
    The file is replaced atomically and left untouched when it cannot be
    read or parsed (DsnError, OSError)."""
    src = Path(path)
    raw = src.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DsnError(f"{src.name} is not UTF-8 text ({exc.reason})", exc.start) from exc
    data = canonicalize(text, seed, src.name if name is None else name).encode("utf-8")
    tmp = src.with_name(src.name + ".canon.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, src)
    finally:
        tmp.unlink(missing_ok=True)
    return hashlib.sha256(data).hexdigest()
