"""The exceptions the schematic API backend raises."""
from __future__ import annotations


class SchematicApiError(Exception):
    """Anything that stops a schematic operation: no KiCad 11 installed, the
    API server not starting, a symbol or pin that does not exist, a write that
    would clobber a file, a KiCad error. The message says what to do next."""


class PinNotFoundError(SchematicApiError):
    """A (ref, pin) that resolves to no placed pin: unknown reference, unknown
    pin number/name, or a pin whose unit is not placed. build_from_netlist
    collects these into ``missing_pins`` instead of writing a file."""
