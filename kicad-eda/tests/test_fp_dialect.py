"""Unit tests for server/tools/fp_dialect.py (F2-05, ROADMAP_DECISIONS D0).

pcbnew.FootprintLoad returns None, silently, for a footprint in a dialect newer
than it reads (KiCad 9.0.4 and a KiCad 10 stock footprint, KiCad 9 and 10 and
a KiCad 11 one; both verified live in tests/test_swig_build_libs_live.py).
fp_dialect turns that None into a message naming the .kicad_mod, its
``(version N)``, the pcbnew build and the newest format that build loads.

Pure tier: stdlib only, no KiCad. The module itself must stay stdlib-only and
Python 3.11-safe (swig_build_board imports it under KiCad 9.0.4's Python).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

import fp_dialect as fd

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "server" / "tools" / "fp_dialect.py"
DIALECT_FIXTURE = ROOT / "tests" / "fixtures" / "dialect" / "Dialect_Test.kicad_mod"
K9, K10, K11 = 20241229, 20260206, 20260901


def footprint(version: int | None, name: str = "R_0603", *, head: str = "footprint",
              newline: str = "\n") -> str:
    """A minimal multi-line .kicad_mod as KiCad 9/10 write it."""
    lines = [f'({head} "{name}"']
    if version is not None:
        lines.append(f"\t(version {version})")
    lines += ['\t(generator "pcbnew")', '\t(layer "F.Cu")',
              '\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))', ")"]
    return newline.join(lines) + newline


def write(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding, newline="")
    return path


# --- footprint_format_version --------------------------------------------------------
@pytest.mark.parametrize("version", [K9, K10, K11, 20211014])
def test_version_of_a_multi_line_footprint(tmp_path: Path, version: int) -> None:
    path = write(tmp_path / "Lib.pretty" / "R.kicad_mod", footprint(version))
    assert fd.footprint_format_version(path) == version
    assert fd.footprint_format_version(str(path)) == version         # str and Path alike


def test_version_of_the_committed_kicad11_fixture() -> None:
    # its name "Dialect_Test (v2)" and descr hold parentheses and an escaped quote
    assert fd.footprint_format_version(DIALECT_FIXTURE) == K11


@pytest.mark.parametrize("text, expected", [
    ('(footprint "X" (version 20211014) (generator pcbnew) (layer "F.Cu"))\n', 20211014),
    ('\ufeff' + footprint(K9, newline="\r\n"), K9),                    # BOM + CRLF (Notepad)
    (footprint(K10, name='X (version 5) \\" y'), K10),               # quoted text is not a header
    ('(module "X" (version 20200829) (layer F.Cu))', 20200829),       # KiCad 6 pre-release root
    ('\n\n  (footprint X\n (version 20241229))', K9),                 # bare name, leading space
])
def test_version_header_variants(tmp_path: Path, text: str, expected: int) -> None:
    assert fd.footprint_format_version(write(tmp_path / "x.kicad_mod", text)) == expected


@pytest.mark.parametrize("text", [
    "(module R_0603 (layer F.Cu) (tedit 5B307E5A)\n  (pad 1 smd rect (at 0 0) (size 1 1)))\n",
    '(footprint "X" (layer "F.Cu") (model "a.wrl" (version 3)))',      # nested: not the header
    '(kicad_pcb (version 20241229) (generator "pcbnew"))',           # a board, not a footprint
    '(footprint "X" (version abc))',
    '(footprint "X" (version 2024',                                   # cut off: no closing paren
    "", "   \n", "not an s-expression", "\x00\x01\x02binary",
])
def test_no_version_for_legacy_or_foreign_text(tmp_path: Path, text: str) -> None:
    assert fd.footprint_format_version(write(tmp_path / "x.kicad_mod", text)) is None


def test_no_version_for_missing_paths_directories_and_unreadable_files(tmp_path: Path) -> None:
    assert fd.footprint_format_version(tmp_path / "missing.kicad_mod") is None
    assert fd.footprint_format_version(tmp_path) is None
    raw = tmp_path / "latin1.kicad_mod"
    raw.write_bytes(b'(footprint "\xe9t\xe9" (version 20241229))')  # not UTF-8: still read
    assert fd.footprint_format_version(raw) == K9


def test_only_the_file_head_is_read(tmp_path: Path) -> None:
    pad = "\t(descr \"" + "x" * (fd.HEADER_BYTES + 100) + "\")\n"
    late = write(tmp_path / "late.kicad_mod", '(footprint "X"\n' + pad + f"\t(version {K9})\n)")
    assert fd.footprint_format_version(late) is None                 # beyond HEADER_BYTES
    early = write(tmp_path / "early.kicad_mod",
                  f'(footprint "X"\n\t(version {K9})\n' + pad + ")")
    assert fd.footprint_format_version(early) == K9                  # the header is first


# --- pcbnew_footprint_limit ------------------------------------------------------------
@pytest.mark.parametrize("build, limit", [
    ("9.0.4", K9), ("9.0.0", K9), ("(9.0.4)", K9), ("9.0.4-1.fc41", K9), ("9.0.0-rc1", K9),
    ("9.0.4-0-g1a2b3c4", K9), ("10.0.5", K10), ("10.0.0", K10), ("(10.0.5-1)", K10),
    ("10.1.0", K10),
])
def test_limit_of_a_release_build(build: str, limit: int) -> None:
    assert fd.pcbnew_footprint_limit(build) == limit


@pytest.mark.parametrize("build", [
    "10.99.0-4025-gdeadbee", "8.99.0", "9.99", "8.0.9", "7.0.11", "11.0.0", "12.0.1",
    "", "unknown", "nightly", "9", None])
def test_limit_unknown_for_nightlies_unsupported_and_garbage(build) -> None:
    assert fd.pcbnew_footprint_limit(build) is None


def test_limits_agree_with_the_rest_of_the_plugin() -> None:
    import fp_author
    import kicad_dialect
    assert fd.KICAD9_FOOTPRINT_VERSION == K9 == kicad_dialect.K9_FP_VERSION
    assert fd.KICAD9_FOOTPRINT_VERSION == fp_author.FORMAT_VERSION    # what we author loads
    assert fd.KICAD10_FOOTPRINT_VERSION == K10
    assert fd.FOOTPRINT_LIMITS == {9: K9, 10: K10}


# --- load_failure_message ----------------------------------------------------------------
def _lib_file(tmp_path: Path, version: int | None, name: str = "Quectel_LG290P") -> Path:
    return write(tmp_path / "libs" / "Proj_parts.pretty" / f"{name}.kicad_mod",
                 footprint(version, name))


@pytest.mark.parametrize("version, build, limit", [
    (K11, "9.0.4", K9), (K11, "10.0.5", K10), (K10, "9.0.4", K9)])
def test_too_new_names_path_version_build_and_limit(tmp_path: Path, version: int, build: str,
                                                    limit: int) -> None:
    path = _lib_file(tmp_path, version)
    msg = fd.load_failure_message(path, build, "Proj_parts:Quectel_LG290P")
    for needle in (str(path), f"(version {version})", f"pcbnew {build}", f"(version {limit})",
                   "Proj_parts:Quectel_LG290P", "newer"):
        assert needle in msg, (needle, msg)
    assert "kicad_dialect" in msg                                   # the way out is named


def test_within_the_limit_says_the_file_itself_is_unreadable(tmp_path: Path) -> None:
    path = _lib_file(tmp_path, K9)
    msg = fd.load_failure_message(path, "9.0.4")
    for needle in (str(path), f"(version {K9})", "pcbnew 9.0.4", f"(version {K9})",
                   "unreadable"):
        assert needle in msg, (needle, msg)
    assert "newer" not in msg


def test_unknown_limit_names_both_known_limits(tmp_path: Path) -> None:
    path = _lib_file(tmp_path, K11)
    msg = fd.load_failure_message(path, "10.99.0-4025-gdeadbee")
    for needle in (str(path), f"(version {K11})", "pcbnew 10.99.0-4025-gdeadbee", "unknown",
                   str(K9), str(K10)):
        assert needle in msg, (needle, msg)


def test_no_header_is_said_so(tmp_path: Path) -> None:
    path = _lib_file(tmp_path, None)
    msg = fd.load_failure_message(path, "9.0.4")
    assert str(path) in msg and "no (version N) header" in msg and f"(version {K9})" in msg


def test_missing_file_and_missing_library(tmp_path: Path) -> None:
    lib = tmp_path / "Proj_parts.pretty"
    lib.mkdir()
    msg = fd.load_failure_message(lib / "Nope.kicad_mod", "9.0.4", "Proj_parts:Nope")
    assert str(lib / "Nope.kicad_mod") in msg and "does not exist" in msg
    assert "Proj_parts:Nope" in msg and "pcbnew 9.0.4" in msg
    gone = tmp_path / "Gone.pretty" / "X.kicad_mod"
    msg = fd.load_failure_message(gone, "10.0.5")
    assert str(tmp_path / "Gone.pretty") in msg and "library" in msg and "does not exist" in msg


@pytest.mark.parametrize("build", [None, "", "  "])
def test_never_raises_on_an_unknown_build(tmp_path: Path, build) -> None:
    msg = fd.load_failure_message(_lib_file(tmp_path, K11), build)
    assert "pcbnew unknown" in msg and f"(version {K11})" in msg


def test_message_is_one_line(tmp_path: Path) -> None:
    for version in (K9, K11, None):
        assert "\n" not in fd.load_failure_message(_lib_file(tmp_path, version), "9.0.4")


# --- module constraints ------------------------------------------------------------------
def test_module_is_small_stdlib_only_and_python311() -> None:
    src = MODULE.read_text(encoding="utf-8")
    assert len(src.splitlines()) < 200
    tree = ast.parse(src, feature_version=(3, 11))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}
    assert "pcbnew" not in imported
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert funcs and all(n.end_lineno - n.lineno < 50 for n in funcs)
    assert all(n.returns is not None for n in funcs)                 # annotated
