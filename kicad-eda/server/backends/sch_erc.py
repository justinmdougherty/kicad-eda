"""ERC through kicad-cli for the schematic API backend.

Always the NEWEST kicad-cli (``kicad_env.pick_cli()``): a file written by
KiCad N only loads in kicad-cli >= N, and the API backend writes KiCad 11
files. The count is taken from each violation's own ``severity`` because
``--severity-error`` filters kicad-cli's console summary but not its JSON
report.
"""
from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path

import kicad_env as env
from backends.errors import SchematicApiError

ERC_TIMEOUT_S = 600
ERROR_SEVERITY = "error"


def erc_report_path(sch_path: str | Path) -> Path:
    """<name>_erc.json beside the schematic (the pipeline's convention)."""
    sch = Path(sch_path)
    return sch.with_name(f"{sch.stem}_erc.json")


def _erc_argv(kicad_cli: Path, sch_path: Path, report: Path) -> list[str]:
    return [str(kicad_cli), "sch", "erc", "--format", "json", "--severity-error",
            "-o", str(report), str(sch_path)]


def _run_cli(argv: list[str], timeout_s: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise SchematicApiError(f"kicad-cli ERC timed out after {timeout_s:g} s") from exc
    except OSError as exc:
        raise SchematicApiError(f"cannot run {argv[0]}: {exc}") from exc


def _read_report(report: Path, proc: subprocess.CompletedProcess) -> dict:
    try:
        return json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        tail = (proc.stdout + proc.stderr)[-800:]
        raise SchematicApiError(
            f"ERC report {report} unreadable ({exc}); kicad-cli exit {proc.returncode}: {tail}"
        ) from exc


def summarize(report: dict) -> dict:
    """{ok, violations, types, by_severity} of a kicad-cli ERC JSON report,
    counting ERROR-severity violations only (warnings inform, never block)."""
    violations = [v for sheet in report.get("sheets", []) for v in sheet.get("violations", [])]
    errors = [v for v in violations if v.get("severity") == ERROR_SEVERITY]
    types = Counter(str(v.get("type", "unknown")) for v in errors)
    by_severity = Counter(str(v.get("severity", "unknown")) for v in violations)
    return {"ok": not errors, "violations": len(errors),
            "types": dict(sorted(types.items())), "by_severity": dict(sorted(by_severity.items()))}


def run_erc(sch_path: str | Path, report_path: str | Path | None = None,
            timeout_s: float = ERC_TIMEOUT_S) -> dict:
    """ERC a saved schematic with the newest kicad-cli. Returns {ok, violations,
    types: {type: count}, by_severity, report, report_path, kicad_cli}."""
    sch = Path(sch_path)
    if not sch.is_file():
        raise SchematicApiError(f"cannot ERC {sch}: file not found (save first)")
    install = env.pick_cli()
    if install is None:
        raise SchematicApiError("no kicad-cli found for ERC; install KiCad or set KICAD_PATH")
    report = Path(report_path) if report_path else erc_report_path(sch)
    proc = _run_cli(_erc_argv(install.kicad_cli, sch, report), timeout_s)
    data = _read_report(report, proc)
    return {**summarize(data), "report": data, "report_path": str(report),
            "kicad_cli": str(install.kicad_cli), "kicad_version": install.version}
