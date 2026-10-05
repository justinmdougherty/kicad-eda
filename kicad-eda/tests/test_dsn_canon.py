"""server/tools/dsn_canon.py: canonical Specctra DSN order before Freerouting (F5-02).

KiCad saves footprints and vias in random-UUID order and the DSN export
follows it; Freerouting 1.9.0 is deterministic for one DSN but order-sensitive
(T07: 5 of 20 buck_5v3a runs failed G4). canonicalize() rewrites the DSN so
the bytes depend on the design only.

Fixtures: tests/fixtures/dsn/buck_r01/r02/r05.dsn.sexpr are three raw KiCad
10.0.5 exports of the SAME placed buck_5v3a board from T07 (different UUID
orders: r01 and r05 failed G4, r02 passed); quoted_names.dsn.sexpr (X-01) has
quoted names with spaces and parentheses and a class net list wrapped across
lines. Unit tier only (no KiCad); the live tier is test_dsn_canon_live.py.
"""
from __future__ import annotations

import ast
import hashlib
import random
import sys
from collections import Counter
from pathlib import Path

import pytest

import dsn_canon as dc
import dsn_sexpr as ds

TOOLS = Path(__file__).resolve().parents[1] / "server" / "tools"
MODULE = TOOLS / "dsn_canon.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "dsn"
BUCK = ("buck_r01", "buck_r02", "buck_r05")
STDLIB = set(sys.stdlib_module_names) | {"dsn_sexpr"}


def _raw(name: str) -> str:
    return (FIXTURES / f"{name}.dsn.sexpr").read_text(encoding="utf-8")


def _section(text: str, name: str) -> list:
    found = ds.find(ds.parse(text), name)
    assert found is not None, name
    return found


def _tokens_without_id(text: str) -> Counter:
    """Every token of the DSN except the (pcb <id>) id."""
    values = [t.value for t in ds.tokenize(text)]
    assert values[:2] == ["(", "pcb"], values[:3]
    return Counter(values[:2] + values[3:])


def _place_refs(text: str) -> list[str]:
    return [p[1] for comp in ds.children(_section(text, "placement"), "component")
            for p in ds.children(comp, "place")]


def _pins(text: str, net: str) -> list[str]:
    for n in ds.children(_section(text, "network"), "net"):
        if n[1] == net:
            return ds.find(n, "pins")[1:]
    raise AssertionError(f"no net {net}")


# ----------------------------------------------------------------------------
# seed 0: the canonical order
# ----------------------------------------------------------------------------
def test_the_raw_fixtures_are_one_board_in_three_orders() -> None:
    raws = [_raw(n) for n in BUCK]
    assert len({hashlib.sha256(r.encode()).hexdigest() for r in raws}) == 3
    assert all(_tokens_without_id(r) == _tokens_without_id(raws[0]) for r in raws)
    assert len({tuple(_place_refs(r)) for r in raws}) == 3          # the orders really differ


def test_permuted_dsns_canonicalise_to_identical_text() -> None:
    outs = {n: dc.canonicalize(_raw(n)) for n in BUCK}
    assert len(set(outs.values())) == 1, {n: hashlib.sha256(o.encode()).hexdigest()[:12]
                                          for n, o in outs.items()}


@pytest.mark.parametrize("seed", [0, 1, 2])
@pytest.mark.parametrize("name", BUCK + ("quoted_names",))
def test_canonicalize_is_idempotent(name: str, seed: int) -> None:
    once = dc.canonicalize(_raw(name), seed=seed)
    assert dc.canonicalize(once, seed=seed) == once
    assert once.endswith(")\n") and not once.endswith("\n\n") and "\r" not in once
    assert ds.parse(ds.emit(ds.parse(once))) == ds.parse(once)


@pytest.mark.parametrize("seed", [0, 1, 2])
@pytest.mark.parametrize("name", BUCK + ("quoted_names",))
def test_canonicalize_keeps_the_token_multiset(name: str, seed: int) -> None:
    raw = _raw(name)
    assert _tokens_without_id(dc.canonicalize(raw, seed=seed)) == _tokens_without_id(raw)


def test_pins_sort_naturally_and_nets_by_name() -> None:
    out = dc.canonicalize(_raw("buck_r05"))
    assert _pins(out, "GND") == ["C1-2", "C2-2", "C3-2", "C4-2", "D1-2", "J1-2", "J2-2",
                                 "U1-3", "U1-3@1", "U1-5"]
    nets = [n[1] for n in ds.children(_section(out, "network"), "net")]
    assert nets == ["GND", "SW", "V5", "VIN", "VIN_RAW"]
    toy = ("(pcb t (network (net B (pins U1-10 U1-3 U1-2 R10-1 R9-1)) (net A (pins X-1))\n"
           "  (class c B A)))")
    canon = dc.canonicalize(toy)
    assert _pins(canon, "B") == ["R9-1", "R10-1", "U1-2", "U1-3", "U1-10"]
    assert [n[1] for n in ds.children(_section(canon, "network"), "net")] == ["A", "B"]
    assert ds.find(_section(canon, "network"), "class") == ["class", "c", "B", "A"]


def test_placement_and_library_follow_image_and_reference_names() -> None:
    out = dc.canonicalize(_raw("buck_r01"))
    comps = ds.children(_section(out, "placement"), "component")
    assert [c[1] for c in comps] == ["CP_Elec_8x10.5", "CP_Elec_10x10.5", "C_0805_2012Metric",
                                     "D_SMA", "Fuse_1812_4532Metric", "L_12x12mm_H6mm",
                                     '"TO-263-5_TabPin3"',
                                     '"TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm_Horizontal"']
    assert _place_refs(out) == ["C3", "C1", "C2", "C4", "D1", "F1", "L1", "U1", "J1", "J2"]
    lib = _section(out, "library")
    kinds = [x[0] for x in lib[1:]]
    assert kinds == ["image"] * 8 + ["padstack"] * (len(kinds) - 8)
    assert [im[1] for im in ds.children(lib, "image")] == [c[1] for c in comps]
    raw_pads = [p[1] for p in ds.children(_section(_raw("buck_r01"), "library"), "padstack")]
    assert [p[1] for p in ds.children(lib, "padstack")] == raw_pads           # kept as written


def test_wiring_sorts_by_emitted_text_and_classes_stay_untouched() -> None:
    out = dc.canonicalize(_raw("buck_r02"))
    wiring = _section(out, "wiring")[1:]
    assert [ds.emit(w) for w in wiring] == sorted(ds.emit(w) for w in wiring)
    raw_classes = ds.children(_section(_raw("buck_r02"), "network"), "class")
    assert ds.children(_section(out, "network"), "class") == raw_classes


def test_structure_zones_sort_within_a_run_only() -> None:
    text = ("(pcb t (structure (layer F.Cu (type signal)) (boundary (path pcb 0 0 0 9 9))\n"
            "  (plane VCC (polygon F.Cu 0 1 1 2 2)) (keepout \"\" (polygon signal 0 5 5 6 6))\n"
            "  (plane GND (polygon F.Cu 0 3 3 4 4)) (via V1) (rule (width 200))))")
    st = _section(dc.canonicalize(text), "structure")
    assert [x[0] for x in st[1:]] == ["layer", "boundary", "keepout", "plane", "plane", "via",
                                      "rule"]
    assert [x[1] for x in st[3:6]] == ['""', "GND", "VCC"]


def test_quoted_names_survive_and_wrapped_class_lists_parse() -> None:
    raw = _raw("quoted_names")
    out = dc.canonicalize(raw)
    nets = [n[1] for n in ds.children(_section(out, "network"), "net")]
    assert nets == sorted(nets, key=lambda t: (dc.natural_key(ds.unquote(t)), t))
    assert {'"Net-(D1-A)"', '"a b"', '"/LED (red)"', '"#RESET"', '/ANT_2"'} <= set(nets)
    assert ds.children(_section(out, "network"), "class") == \
        ds.children(_section(raw, "network"), "class")
    assert "(place D1 110000 -85000 front 0\n" in out and '(PN "LED (red)"))' in out
    assert '(net "Net-(D1-A)")' in out


# ----------------------------------------------------------------------------
# the (pcb <id>) id
# ----------------------------------------------------------------------------
def test_pcb_id_becomes_the_dsn_basename() -> None:
    raw = _raw("buck_r01")
    assert raw.startswith("(pcb /work/netlist.dsn\n")
    assert dc.canonicalize(raw).startswith("(pcb netlist.dsn\n")
    assert dc.canonicalize(raw, name="board.dsn").startswith("(pcb board.dsn\n")
    assert dc.canonicalize(raw, name="my board-2.dsn").startswith('(pcb "my board-2.dsn"\n')
    assert dc.canonicalize(raw, name='a"b.dsn').startswith("(pcb a_b.dsn\n")
    win = raw.replace("/work/netlist.dsn", '"C:\\Users\\a b\\proj\\netlist.dsn"', 1)
    assert dc.canonicalize(win) == dc.canonicalize(raw)


def test_a_dsn_without_sections_is_accepted() -> None:
    assert dc.canonicalize("(pcb dsn)") == "(pcb dsn)\n"                 # test_snapshot's fake
    assert dc.canonicalize("(pcb dsn)", name="netlist.dsn") == "(pcb netlist.dsn)\n"
    assert dc.canonicalize("(pcb)", name="x.dsn") == "(pcb x.dsn)\n"
    assert dc.canonicalize("(pcb x (placement) (network))", seed=3) == \
        "(pcb x\n  (placement)\n  (network))\n"


@pytest.mark.parametrize("text", ["", "garbage", "(pcb x (placement", "(pcb x))",
                                  '(pcb x (net "open))', "(pcb a) (pcb b)", "(session s)",
                                  "((pcb x))", "(pcb x (network) (wiring) (network))"])
def test_malformed_or_foreign_text_raises_dsn_error(text: str) -> None:
    with pytest.raises(ds.DsnError):
        dc.canonicalize(text)
    assert dc.DsnError is ds.DsnError


@pytest.mark.parametrize("seed, exc", [(-1, ValueError), (True, TypeError), (1.0, TypeError),
                                       ("1", TypeError)])
def test_a_bad_seed_is_refused(seed: object, exc: type) -> None:
    with pytest.raises(exc):
        dc.canonicalize("(pcb x)", seed=seed)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------
# seed k > 0: a deterministic alternate footprint order
# ----------------------------------------------------------------------------
def test_seeds_differ_from_zero_and_reproduce_from_any_input_order() -> None:
    raws = [_raw(n) for n in BUCK]
    base = dc.canonicalize(raws[0])
    one = {dc.canonicalize(r, seed=1) for r in raws}
    two = {dc.canonicalize(r, seed=2) for r in raws}
    assert len(one) == 1 and len(two) == 1
    assert len({base, *one, *two}) == 3
    assert dc.canonicalize(raws[0], seed=1) == dc.canonicalize(raws[0], seed=1)


def test_footprint_order_is_a_seeded_shuffle_of_the_sorted_references() -> None:
    raw = _raw("buck_r02")
    refs = ["C1", "C2", "C3", "C4", "D1", "F1", "J1", "J2", "L1", "U1"]
    assert dc.footprint_order(raw, 0) == refs
    for seed in (1, 2, 7):
        expected = list(refs)
        random.Random(seed).shuffle(expected)
        assert dc.footprint_order(raw, seed) == expected
        assert dc.footprint_order(_raw("buck_r05"), seed) == expected


def _rank(order: list[str]) -> dict[str, int]:
    return {ref: k for k, ref in enumerate(order)}


@pytest.mark.parametrize("seed", [1, 2, 5])
def test_seed_applies_the_order_like_kicads_footprint_iteration(seed: int) -> None:
    raw = _raw("buck_r01")
    rank = _rank(dc.footprint_order(raw, seed))
    out = dc.canonicalize(raw, seed=seed)
    comps = ds.children(_section(out, "placement"), "component")
    firsts = [rank[ds.children(c, "place")[0][1]] for c in comps]
    assert firsts == sorted(firsts)                                     # groups by first ref
    for comp in comps:
        places = [rank[p[1]] for p in ds.children(comp, "place")]
        assert places == sorted(places)
    images = [im[1] for im in ds.children(_section(out, "library"), "image")]
    assert images == [c[1] for c in comps]                              # images by first use
    image_pins = {im[1]: [next(x for x in p[2:] if isinstance(x, str))
                          for p in ds.children(im, "pin")]
                  for im in ds.children(_section(out, "library"), "image")}
    ref_image = {p[1]: c[1] for c in comps for p in ds.children(c, "place")}
    net_firsts = []
    for net in ds.children(_section(out, "network"), "net"):
        keys = [(rank[ref], image_pins[ref_image[ref]].index(pad))
                for ref, pad in (pin.split("-", 1) for pin in ds.find(net, "pins")[1:])]
        assert keys == sorted(keys), net
        net_firsts.append(keys[0])
    assert net_firsts == sorted(net_firsts)                             # nets by first pin


def test_seed_handles_quoted_references_and_pins() -> None:
    text = ('(pcb t (placement (component "R 0603" (place "R 2" 1 1 front 0)'
            ' (place "R 1" 2 2 front 0)))\n'
            ' (library (image "R 0603" (pin P "A-1" 0 0) (pin P 2 1 0)))\n'
            ' (network (net N1 (pins "R 2"-2 "R 1"-"A-1" "R 2"-"A-1"))))')
    order = dc.footprint_order(text, 4)
    assert sorted(order) == ["R 1", "R 2"]
    out = dc.canonicalize(text, seed=4)
    first, second = ('"R 1"', '"R 2"') if order == ["R 1", "R 2"] else ('"R 2"', '"R 1"')
    assert _pins(out, "N1") == [p for p in (f'{first}-"A-1"', f"{first}-2",
                                            f'{second}-"A-1"', f"{second}-2")
                                if p != '"R 1"-2']


# ----------------------------------------------------------------------------
# canonicalize_file
# ----------------------------------------------------------------------------
def test_canonicalize_file_rewrites_in_place_and_returns_the_sha256(tmp_path: Path) -> None:
    dsn = tmp_path / "netlist.dsn"
    dsn.write_bytes(_raw("buck_r05").replace("\n", "\r\n").encode("utf-8"))   # KiCad 9 on Windows
    digest = dc.canonicalize_file(dsn)
    data = dsn.read_bytes()
    assert digest == hashlib.sha256(data).hexdigest()
    assert data.decode("utf-8") == dc.canonicalize(_raw("buck_r01"), name="netlist.dsn")
    assert b"\r" not in data and data.startswith(b"(pcb netlist.dsn\n")
    assert dc.canonicalize_file(str(dsn)) == digest and dsn.read_bytes() == data
    assert sorted(p.name for p in tmp_path.iterdir()) == ["netlist.dsn"]       # no temp left
    seeded = dc.canonicalize_file(dsn, seed=2, name="other.dsn")
    assert seeded != digest and dsn.read_text(encoding="utf-8").startswith("(pcb other.dsn\n")


def test_canonicalize_file_leaves_a_bad_file_alone(tmp_path: Path) -> None:
    bad = tmp_path / "bad.dsn"
    bad.write_bytes(b"(pcb x (placement")
    with pytest.raises(ds.DsnError):
        dc.canonicalize_file(bad)
    assert bad.read_bytes() == b"(pcb x (placement"
    latin = tmp_path / "latin.dsn"
    latin.write_bytes(b"(pcb x (net \xe9))")
    with pytest.raises(ds.DsnError, match="UTF-8"):
        dc.canonicalize_file(latin)
    with pytest.raises(OSError):
        dc.canonicalize_file(tmp_path / "missing.dsn")


# ----------------------------------------------------------------------------
# module constraints
# ----------------------------------------------------------------------------
def test_module_is_stdlib_only_py311_and_small() -> None:
    source = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 11))
    imported = {(a.name if isinstance(n, ast.Import) else n.module).split(".")[0]
                for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in n.names}
    assert imported <= STDLIB, imported - STDLIB
    assert "re" not in imported                                # no regexes on DSN structure
    assert source.count("\n") < 400
    assert "canonicalize_file" in (ast.get_docstring(tree) or "")
