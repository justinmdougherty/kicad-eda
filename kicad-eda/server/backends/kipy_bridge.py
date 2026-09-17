"""Guarded import of the vendored kicad-python (kipy) plus the enum tables the
schematic backend needs.

``backends/__init__.py`` has already put ``server/vendor`` first on sys.path.
When kipy or one of its runtime deps (protobuf, pynng, zstandard, ...) is
missing this module still imports: ``IMPORT_ERROR`` holds the reason and
``require()`` turns it into a SchematicApiError that says what to install.
Every other backend module reaches kipy through this one, so a broken install
is reported once and clearly instead of as an ImportError inside a tool call.
"""
from __future__ import annotations

from backends.errors import SchematicApiError

IMPORT_ERROR: str | None = None
try:
    from kipy import KiCad
    from kipy.errors import ApiError, LibraryCommandError
    from kipy.errors import ConnectionError as KiCadConnectionError
    from kipy.geometry import Vector2
    from kipy.kicad import LibraryLoadStatus
    from kipy.proto.common.types.base_types_pb2 import DocumentType, ElectricalPinType
    from kipy.proto.schematic import schematic_jobs_pb2
    from kipy.schematic import Schematic
    import kipy.common_types as ct
    import kipy.schematic_types as st
except Exception as exc:  # noqa: BLE001 - any failure means "no API backend"; the reason is kept
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

# re-exported for the other backend modules (kb.DocumentType, kb.st, ...)
__all__ = ["IMPORT_ERROR", "INSTALL_HINT", "KIPY_EXCEPTIONS", "ORIENT_TO_DEG", "DEG_TO_ORIENT",
           "PIN_TYPE_NAMES", "require", "vec", "label_shape", "KiCad", "ApiError",
           "LibraryCommandError", "KiCadConnectionError", "Vector2", "LibraryLoadStatus",
           "DocumentType", "ElectricalPinType", "schematic_jobs_pb2", "Schematic", "ct", "st"]

INSTALL_HINT = ("install the runtime deps of the vendored kipy with "
                "`pip install -r server/requirements.txt` (protobuf, pynng, jsonschema, "
                "zstandard, typing_extensions) and make sure no PyPI kicad-python shadows "
                "server/vendor/kipy")


def require() -> None:
    """Raise a SchematicApiError when kipy could not be imported."""
    if IMPORT_ERROR:
        raise SchematicApiError(f"vendored kipy is not importable ({IMPORT_ERROR}); {INSTALL_HINT}")


# Exceptions a kipy call can raise; a backend method wraps these into
# SchematicApiError with context about what it was doing.
if IMPORT_ERROR is None:
    KIPY_EXCEPTIONS: tuple[type[BaseException], ...] = (
        ApiError, KiCadConnectionError, LibraryCommandError, TimeoutError, OSError, ValueError)
else:
    KIPY_EXCEPTIONS = (TimeoutError, OSError, ValueError)


def _orientation_tables() -> tuple[dict, dict]:
    sso = st.SchematicSymbolOrientation
    to_deg = {sso.SSO_0: 0, sso.SSO_90: 90, sso.SSO_180: 180, sso.SSO_270: 270}
    return to_deg, {deg: orient for orient, deg in to_deg.items()}


def _pin_type_table() -> dict:
    """API electrical type -> the .kicad_sym token symlib.pin_etypes() yields,
    so the API and file backends share one power-flag rule."""
    ept = ElectricalPinType
    return {ept.EPT_INPUT: "input", ept.EPT_OUTPUT: "output",
            ept.EPT_BIDIRECTIONAL: "bidirectional", ept.EPT_TRISTATE: "tri_state",
            ept.EPT_PASSIVE: "passive", ept.EPT_FREE: "free",
            ept.EPT_UNSPECIFIED: "unspecified", ept.EPT_POWER_INPUT: "power_in",
            ept.EPT_POWER_OUTPUT: "power_out", ept.EPT_OPEN_COLLECTOR: "open_collector",
            ept.EPT_OPEN_EMITTER: "open_emitter", ept.EPT_NO_CONNECT: "no_connect",
            ept.EPT_UNKNOWN: "unknown"}


if IMPORT_ERROR is None:
    ORIENT_TO_DEG, DEG_TO_ORIENT = _orientation_tables()
    PIN_TYPE_NAMES = _pin_type_table()
else:
    ORIENT_TO_DEG, DEG_TO_ORIENT, PIN_TYPE_NAMES = {}, {}, {}


def vec(x_mm: float, y_mm: float):
    """A kipy Vector2 from millimetres, rounded to the nearest nanometre
    (Vector2.from_xy_mm truncates, which can put a wire end 1 nm off a pin)."""
    return Vector2.from_xy(int(round(x_mm * 1_000_000)), int(round(y_mm * 1_000_000)))


def label_shape(name: str):
    """SchematicLabelShape value for a short name ('passive', 'input', ...)."""
    key = f"SLSH_{name.upper()}"
    try:
        return st.SchematicLabelShape.Value(key)
    except ValueError as exc:
        names = [k[5:].lower() for k in st.SchematicLabelShape.keys() if k != "SLSH_UNKNOWN"]
        raise SchematicApiError(f"unknown label shape {name!r}; one of {names}") from exc
