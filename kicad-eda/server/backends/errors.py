"""The exceptions the KiCad API backends raise."""
from __future__ import annotations


class SchematicApiError(Exception):
    """Anything that stops a schematic operation: no KiCad 11 installed, the
    API server not starting, a symbol or pin that does not exist, a write that
    would clobber a file, a KiCad error. The message says what to do next."""


class PinNotFoundError(SchematicApiError):
    """A (ref, pin) that resolves to no placed pin: unknown reference, unknown
    pin number/name, or a pin whose unit is not placed. build_from_netlist
    collects these into ``missing_pins`` instead of writing a file."""


class BoardApiError(Exception):
    """Anything that stops a board operation: no KiCad 11 for a headless
    session, the api-server not starting, the board not open in the GUI, a
    net / copper layer / item id the board does not have, a malformed segment
    or via spec, connectivity the connected KiCad cannot answer, a KiCad
    error. The message says what to do next (it names the valid nets or
    layers when the caller passed an unknown one)."""
