"""Freerouting command line: no confirmation dialog may wait on a click."""
from __future__ import annotations

import board_pipeline_tools as bpt


def _args(jar: str) -> list[str]:
    args = bpt._freerouting_args("java", jar, "in.dsn", "out.ses", 20, 600)
    assert isinstance(args, list), args
    return args


def test_v1_skips_the_autorouter_countdown_dialog() -> None:
    args = _args("/x/freerouting-1.9.0.jar")
    i = args.index("-dct")
    assert args[i + 1] == "0"


def test_v2_passes_dct_once_and_stays_headless() -> None:
    args = _args("/x/freerouting-2.1.0.jar")
    assert args.count("-dct") == 1
    assert "--gui.enabled=false" in args
