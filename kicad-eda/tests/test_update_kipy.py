"""The vendored kipy fix for ``KiCad.reload_library`` and the update_kipy step
that re-applies it on every refresh.

Upstream kicad-python validated ``type == LibraryTableScope.LTS_BOTH`` where it
meant ``scope``: LT_DESIGN_BLOCK == LTS_BOTH == 3, so a valid design-block
reload was refused and ``scope=LTS_BOTH`` went to KiCad unvalidated. The fix
lives in server/vendor/kipy/kicad.py and in update_kipy.KIPY_PATCHES, so
re-vendoring cannot silently drop it.

The reload tests drive the vendored KiCad object through a fake client (no
KiCad needed) and skip only when kipy's runtime deps are missing.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import update_kipy as uk
from backends import kipy_bridge as kb

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "server" / "vendor"
VENDORED_KICAD_PY = VENDOR / "kipy" / "kicad.py"

requires_kipy = pytest.mark.skipif(bool(kb.IMPORT_ERROR), reason=kb.IMPORT_ERROR or "")

if kb.IMPORT_ERROR is None:
    from kipy.kicad import KiCad, LibraryCommandStatus, LibraryTableScope, LibraryType

BUGGY = 'if type == LibraryTableScope.LTS_BOTH:\n            raise ValueError("LTS_BOTH is not valid'
FIXED = 'if scope == LibraryTableScope.LTS_BOTH:\n            raise ValueError("LTS_BOTH is not valid'


class _FakeClient:
    """Records every command; answers each with an LCS_OK status."""

    def __init__(self) -> None:
        self.sent: list = []

    def send(self, command, _response_type):
        self.sent.append(command)
        return LibraryCommandStatus(code=LibraryCommandStatus.Code.LCS_OK)

    def close(self) -> None:
        pass


# --- the vendored KiCad.reload_library ---------------------------------------

@requires_kipy
@pytest.mark.parametrize("lib_type", ["LT_SYMBOL", "LT_FOOTPRINT", "LT_DESIGN_BLOCK"])
def test_reload_library_rejects_the_lts_both_scope(lib_type: str):
    client = _FakeClient()
    kicad = KiCad.from_client(client)
    with pytest.raises(ValueError, match="LTS_BOTH is not valid for reload_library"):
        kicad.reload_library(getattr(LibraryType, lib_type), LibraryTableScope.LTS_BOTH)
    assert client.sent == []


@requires_kipy
@pytest.mark.parametrize("lib_type", ["LT_SYMBOL", "LT_FOOTPRINT", "LT_DESIGN_BLOCK"])
@pytest.mark.parametrize("scope", ["LTS_GLOBAL", "LTS_PROJECT"])
def test_reload_library_sends_every_type_for_one_table(lib_type: str, scope: str):
    # LT_DESIGN_BLOCK (3) used to be refused: it equals LTS_BOTH (3)
    client = _FakeClient()
    KiCad.from_client(client).reload_library(getattr(LibraryType, lib_type),
                                             getattr(LibraryTableScope, scope), ["lib_a"])
    (command,) = client.sent
    assert command.type == getattr(LibraryType, lib_type)
    assert command.scope == getattr(LibraryTableScope, scope)
    assert list(command.nickname) == ["lib_a"]


# --- update_kipy re-applies the fix ------------------------------------------

def test_vendored_kicad_py_carries_every_patch():
    text = VENDORED_KICAD_PY.read_text(encoding="utf-8")
    assert BUGGY not in text and text.count(FIXED) == 1
    for patch in uk.KIPY_PATCHES:
        patched = (VENDOR / "kipy" / patch.path).read_text(encoding="utf-8")
        assert patch.old not in patched and patched.count(patch.new) == 1, patch.path


def _upstream_kipy(tmp_path: Path, newline: str = "\n") -> Path:
    """A kipy dir whose kicad.py is the vendored one with the bug put back."""
    kipy_dir = tmp_path / "kipy"
    kipy_dir.mkdir()
    text = VENDORED_KICAD_PY.read_text(encoding="utf-8").replace(FIXED, BUGGY)
    assert BUGGY in text
    with (kipy_dir / "kicad.py").open("w", encoding="utf-8", newline="") as f:
        f.write(text.replace("\n", newline))
    return kipy_dir


def test_apply_patches_restores_the_vendored_fix(tmp_path: Path):
    kipy_dir = _upstream_kipy(tmp_path)
    assert uk.apply_patches(kipy_dir) == ["kicad.py: patched"]
    assert (kipy_dir / "kicad.py").read_bytes() == \
        VENDORED_KICAD_PY.read_text(encoding="utf-8").encode("utf-8")


def test_apply_patches_is_idempotent(tmp_path: Path):
    kipy_dir = _upstream_kipy(tmp_path)
    uk.apply_patches(kipy_dir)
    before = (kipy_dir / "kicad.py").read_bytes()
    assert uk.apply_patches(kipy_dir) == ["kicad.py: already patched"]
    assert (kipy_dir / "kicad.py").read_bytes() == before


def test_apply_patches_keeps_crlf_line_endings(tmp_path: Path):
    kipy_dir = _upstream_kipy(tmp_path, newline="\r\n")
    assert uk.apply_patches(kipy_dir) == ["kicad.py: patched"]
    raw = (kipy_dir / "kicad.py").read_bytes()
    assert FIXED.replace("\n", "\r\n").encode() in raw
    assert b"\r\n" in raw and raw.count(b"\n") == raw.count(b"\r\n")


def test_apply_patches_refuses_when_upstream_changed_the_code(tmp_path: Path):
    kipy_dir = tmp_path / "kipy"
    kipy_dir.mkdir()
    (kipy_dir / "kicad.py").write_text("def reload_library():\n    pass\n", encoding="utf-8")
    with pytest.raises(uk.UpdateError, match=r"kicad\.py.*no longer applies"):
        uk.apply_patches(kipy_dir)


def test_apply_patches_refuses_an_ambiguous_match(tmp_path: Path):
    kipy_dir = tmp_path / "kipy"
    kipy_dir.mkdir()
    (kipy_dir / "kicad.py").write_text(f"{BUGGY}\n{BUGGY}\n", encoding="utf-8")
    with pytest.raises(uk.UpdateError, match="no longer applies"):
        uk.apply_patches(kipy_dir)
    assert (kipy_dir / "kicad.py").read_text(encoding="utf-8") == f"{BUGGY}\n{BUGGY}\n"


def test_apply_patches_refuses_a_missing_file(tmp_path: Path):
    with pytest.raises(uk.UpdateError, match="missing"):
        uk.apply_patches(tmp_path)


def _swap_in_place(path: Path, old: str, new: str) -> None:
    """Replace ``old`` with ``new`` in ``path``, keeping the file's own line
    endings (the working copy may be CRLF under git autocrlf, CI is LF)."""
    with path.open(encoding="utf-8", newline="") as f:
        text = f.read()
    nl = "\r\n" if "\r\n" in text else "\n"
    swapped = text.replace(old.replace("\n", nl), new.replace("\n", nl))
    assert swapped != text, f"{old!r} not found in {path}"
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(swapped)


@requires_kipy
def test_skip_generate_revendor_reapplies_the_fix(tmp_path: Path):
    """End to end: re-vendoring an UPSTREAM checkout (bug present) with
    --skip-generate installs a kicad.py byte-identical to the vendored one."""
    checkout = tmp_path / "kicad-python"
    shutil.copytree(VENDOR / "kipy", checkout / "kipy", ignore=uk.COPY_IGNORE)
    _swap_in_place(checkout / "kipy" / "kicad.py", FIXED, BUGGY)
    shutil.copyfile(VENDOR / "kipy-LICENSE", checkout / "LICENSE")
    (checkout / "pyproject.toml").write_text('[project]\nversion = "0.9.0.dev0"\n',
                                             encoding="utf-8")
    dest = tmp_path / "vendor"
    rc = uk.main(["--checkout", str(checkout), "--skip-generate", "--dest", str(dest),
                  "--work", str(tmp_path / "work"), "--fetched", "2026-10-04"])
    assert rc == 0
    assert (dest / "kipy" / "kicad.py").read_bytes() == VENDORED_KICAD_PY.read_bytes()
    assert BUGGY in (checkout / "kipy" / "kicad.py").read_text(encoding="utf-8")  # input untouched
