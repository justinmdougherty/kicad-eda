"""Locate a KiCad installation for the tests that need its real symbol
libraries or kicad-cli.

Everything here is optional: a missing install turns those tests into skips,
never failures, so the pure-Python tier still runs on a bare CI runner.

Search order (first root whose share/kicad/symbols/Device.kicad_sym exists):
  1. ``$KICAD_PATH``            — the same override kicad_server honours
  2. C:/Program Files/KiCad/9.0 — the version the plugin dev environment pins
  3. the install owning ``kicad-cli`` on PATH (``<root>/bin/kicad-cli``)
  4. /usr                       — distro packages and the kicad/kicad docker image
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

_WINDOWS_DEFAULT_ROOT = Path("C:/Program Files/KiCad/9.0")
_CLI_NAME = "kicad-cli.exe" if os.name == "nt" else "kicad-cli"


@dataclass(frozen=True)
class KicadInstall:
    root: Path
    symbols: Path
    cli: Path | None


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []
    env_root = os.environ.get("KICAD_PATH")
    if env_root:
        roots.append(Path(env_root))
    if os.name == "nt":
        roots.append(_WINDOWS_DEFAULT_ROOT)
    on_path = shutil.which("kicad-cli")
    if on_path:
        roots.append(Path(on_path).resolve().parent.parent)
    roots.append(Path("/usr"))
    return roots


def _install_at(root: Path) -> KicadInstall | None:
    symbols = root / "share" / "kicad" / "symbols"
    if not (symbols / "Device.kicad_sym").is_file():
        return None
    cli = root / "bin" / _CLI_NAME
    return KicadInstall(root=root, symbols=symbols, cli=cli if cli.is_file() else None)


def find_kicad() -> KicadInstall | None:
    """The first usable KiCad install, or None when none is present."""
    for root in _candidate_roots():
        found = _install_at(root)
        if found is not None:
            return found
    return None


KICAD = find_kicad()

requires_symbols = pytest.mark.skipif(
    KICAD is None, reason="KiCad symbol libraries not installed")
requires_kicad_cli = pytest.mark.skipif(
    KICAD is None or KICAD.cli is None, reason="kicad-cli not installed")
