"""Live tier for server/tools/dsn_canon.py (F5-02).

Skipped without a KiCad install whose bundled Python imports pcbnew.

- dsn_sexpr and dsn_canon import under the OLDEST install's Python (KiCad 9.0
  ships 3.11) and canonicalise the T07 buck fixture to the same bytes as the
  venv does.
- examples/lm358_buffer is built twice with build_board_from_netlist into two
  scratch directories, each board is exported with swig_export_dsn.py, and
  canonicalize_file turns both exports into identical bytes. The builder
  seeds KiCad's UUID generator from sha256(netlist bytes), so the second
  build reads the SAME design re-serialised with other whitespace: its
  footprints get other UUIDs and KiCad saves (and exports) them in another
  order. The test tries a few layouts until the raw footprint order really
  differs (deterministic while the seeding stays; a 23/24 chance per try if
  UUIDs were random again).
- The same design with C2 at 90 deg: KiCad writes two C_0603 images and
  names the one it meets second <footprint>::1, so the raw naming follows the
  UUID order; builds with both namings canonicalise to identical bytes.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import board_pipeline_tools as bpt
import dsn_canon as dc
import dsn_sexpr as ds
import kicad_env as env
import toolchain as tc
from fixtures.swig_python import require_kicad_python

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "server" / "tools"
EXAMPLE = ROOT / "examples" / "lm358_buffer" / "netlist.json"
BUCK = Path(__file__).resolve().parent / "fixtures" / "dsn" / "buck_r01.dsn.sexpr"

_IMPORT = ("import hashlib, sys; sys.path.insert(0, sys.argv[1]); import dsn_sexpr, dsn_canon; "
           "text = open(sys.argv[2], encoding='utf-8').read(); "
           "print(sys.version_info[:2], hashlib.sha256("
           "dsn_canon.canonicalize(text, seed=int(sys.argv[3])).encode('utf-8')).hexdigest())")


def _oldest_kicad_python() -> str:
    found = [i for i in env.discover() if i.python is not None]
    if not found:
        pytest.skip(f"no KiCad python with pcbnew: {env.probe_errors()}")
    return str(min(found, key=lambda i: i.version_tuple).python)


def _run(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)


@pytest.mark.parametrize("seed", [0, 2])
def test_live_modules_import_and_agree_under_kicad_python(seed: int) -> None:
    kpy = _oldest_kicad_python()
    r = _run([kpy, "-c", _IMPORT, str(TOOLS), str(BUCK), str(seed)])
    assert r.returncode == 0, r.stderr[-2000:]
    version, digest = r.stdout.strip().splitlines()[-1].rsplit(" ", 1)
    assert version.startswith("(3, ")
    expected = dc.canonicalize(BUCK.read_text(encoding="utf-8"), seed=seed)
    assert digest == hashlib.sha256(expected.encode("utf-8")).hexdigest()


def _build_and_export(work: Path, kpy: str, indent: int | None,
                      design: dict | None = None) -> tuple[str, Path]:
    """`design` (default: examples/lm358_buffer) built into `work` and
    exported; (raw DSN, path). The example with indent None is copied byte for
    byte; otherwise the design is serialised with that JSON indent (other
    bytes, so other footprint UUIDs)."""
    work.mkdir()
    netlist = work / "netlist.json"
    if design is None and indent is None:
        shutil.copyfile(EXAMPLE, netlist)
    else:
        design = design or json.loads(EXAMPLE.read_text(encoding="utf-8"))
        netlist.write_text(json.dumps(design, indent=indent) + "\n", encoding="utf-8")
    pcb = work / "netlist.kicad_pcb"
    built = bpt.build_board_from_netlist(str(netlist), 50, 25, out_pcb=str(pcb))
    assert built.get("ok"), built
    dsn = work / "netlist.dsn"
    r = _run([kpy, str(TOOLS / "swig_export_dsn.py"), str(pcb), str(dsn)], timeout=120)
    assert dsn.is_file(), (r.stdout + r.stderr)[-2000:]
    return dsn.read_text(encoding="utf-8"), dsn


def _footprint_uuids(pcb: Path) -> list[str]:
    """The footprint uuids in file order (the order KiCad iterates them in)."""
    tree = ds.parse_nodes(pcb.read_text(encoding="utf-8"))[0]
    uuids = []
    for fp in ds.find_all(tree, "footprint"):
        node = ds.child(fp, "uuid")
        uuids += [node.items[0].value] if node and node.items else []
    return uuids


def _body(raw: str) -> str:
    """The DSN without its first line, the (pcb <absolute path>) id."""
    return raw.split("\n", 1)[1]


def _exported_order(raw: str) -> list[str]:
    """The references in the order the raw export writes their places (the
    footprint order KiCad iterated)."""
    placement = ds.find(ds.parse(raw), "placement") or []
    return [p[1] for comp in ds.children(placement, "component")
            for p in ds.children(comp, "place")]


def test_live_two_builds_canonicalise_to_identical_bytes(tmp_path: Path) -> None:
    kpy = require_kicad_python()
    raw_a, dsn_a = _build_and_export(tmp_path / "a", kpy, None)
    tried = []
    for indent in (4, 1, 3, 6):                       # same design, other netlist bytes
        work = tmp_path / f"b{indent}"
        raw_b, dsn_b = _build_and_export(work, kpy, indent)
        tried.append(indent)
        if _exported_order(raw_b) != _exported_order(raw_a):
            break
    order_a, order_b = _exported_order(raw_a), _exported_order(raw_b)
    assert order_a != order_b, f"every layout {tried} exported the footprints in one order"
    assert sorted(order_a) == sorted(order_b) and _body(raw_a) != _body(raw_b)
    uuids_a = _footprint_uuids(tmp_path / "a" / "netlist.kicad_pcb")
    uuids_b = _footprint_uuids(work / "netlist.kicad_pcb")
    assert len(uuids_a) == 4 and set(uuids_a).isdisjoint(uuids_b)   # independent UUIDs
    sha_a, sha_b = dc.canonicalize_file(dsn_a), dc.canonicalize_file(dsn_b)
    assert sha_a == sha_b and dsn_a.read_bytes() == dsn_b.read_bytes()
    assert dsn_a.read_text(encoding="utf-8").startswith("(pcb netlist.dsn\n")
    canon = dsn_a.read_text(encoding="utf-8")
    for seed in (1, 2):
        assert dc.canonicalize(raw_a, seed=seed) == dc.canonicalize(raw_b, seed=seed)
        assert dc.canonicalize(raw_a, seed=seed, name="netlist.dsn") != canon
    print(json.dumps({"sha256": sha_a, "layouts_tried": tried, "raw_orders": [order_a, order_b],
                      "kicad_python": kpy, "pcbnew": tc.kicad_python()}))


def _image_of(raw: str) -> dict[str, str]:
    """reference -> the image id its placement component names."""
    placement = ds.find(ds.parse(raw), "placement") or []
    return {p[1]: comp[1] for comp in ds.children(placement, "component")
            for p in ds.children(comp, "place")}


def test_live_rotated_duplicates_canonicalise_to_identical_bytes(tmp_path: Path) -> None:
    """C2 at 90 deg: KiCad writes a second C_0603 image and names the one it
    meets second <footprint>::1, so which capacitor carries the suffix follows
    the UUID order; the canonical DSN names them by reference (C1 first)."""
    kpy = require_kicad_python()
    example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    design = {**example, "components": [{**c, "rot": 90} if c["ref"] == "C2" else c
                                        for c in example["components"]]}
    by_naming: dict[str, Path] = {}
    for indent in (None, 1, 2, 3, 4, 5, 6, 7, 8):
        raw, dsn = _build_and_export(tmp_path / f"r{indent}", kpy, indent, design)
        images = _image_of(raw)
        assert images["C1"] != images["C2"], images       # two different C_0603 images
        by_naming.setdefault(images["C1"], dsn)
        if len(by_naming) == 2:
            break
    assert len(by_naming) == 2, f"C1 always used {list(by_naming)}: the order never flipped"
    first, second = by_naming.values()
    assert dc.canonicalize_file(first) == dc.canonicalize_file(second)
    assert first.read_bytes() == second.read_bytes()
    footprint = next(c["footprint"] for c in example["components"] if c["ref"] == "C1")
    canon = _image_of(first.read_text(encoding="utf-8"))
    assert (canon["C1"], canon["C2"]) == (footprint, f"{footprint}::1")
    print(json.dumps({"raw_C1_images": list(by_naming), "canonical": canon}))
