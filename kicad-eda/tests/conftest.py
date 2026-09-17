"""Shared fixtures for the unit tier.

The example netlists under examples/ are used IN PLACE as fixtures (never
copied): every design fixture hands the test a deep copy of the parsed JSON so a
test may mutate it freely without affecting other tests.
"""
from __future__ import annotations

import copy
import functools
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


@functools.lru_cache(maxsize=None)
def _load_example(name: str) -> dict:
    path = EXAMPLES / name / "netlist.json"
    if not path.is_file():
        raise FileNotFoundError(f"example netlist missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_example(name: str) -> dict:
    """A fresh deep copy of examples/<name>/netlist.json."""
    return copy.deepcopy(_load_example(name))


@pytest.fixture(scope="session")
def examples_dir() -> Path:
    return EXAMPLES


@pytest.fixture(scope="session")
def example_netlists(examples_dir: Path) -> list[Path]:
    paths = sorted(examples_dir.glob("*/netlist.json"))
    assert paths, f"no example netlists under {examples_dir}"
    return paths


@pytest.fixture
def buck_design() -> dict:
    """examples/buck_5v3a — 10 parts, 5 nets, Class 3, full net_specs."""
    return load_example("buck_5v3a")


@pytest.fixture
def hat_design() -> dict:
    """examples/rpi5_battery_hat — 68 parts, 36 nets, Class 2, fixed placement,
    preroutes, no_connect list, partial net_specs."""
    return load_example("rpi5_battery_hat")


@pytest.fixture
def load_design():
    """Factory fixture: load_design("lm358_buffer") -> fresh design dict."""
    return load_example
