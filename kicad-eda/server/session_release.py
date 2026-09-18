"""One headless KiCad api-server holds ONE document at a time.

The schematic registry (kicad_server) and the board registry
(board_route_tools) both drop idle (saved) headless sessions before opening
another document and refuse while one has unsaved changes. ``Release`` is
what such a sweep reports; ``fields()`` are the extra keys the tool that
triggered it adds to its response: the released paths (``released_sessions``
for schematics, ``released_boards`` for boards), a note saying how to reopen
one, and any close errors. ``close_error_fields`` reports a session that did
not stop cleanly when its path is re-opened, so a dead api-server is never
dropped silently.
"""
from __future__ import annotations

from dataclasses import dataclass

# kind -> (released-paths key, close-errors key, how to get a released document back)
KINDS: dict[str, tuple[str, str, str]] = {
    "session": ("released_sessions", "released_session_close_errors",
                "the headless api-server holds one schematic at a time; load_schematic a "
                "released path again to keep editing it"),
    "board": ("released_boards", "released_board_close_errors",
              "the headless api-server holds one board at a time; open_board a released "
              "path again to keep routing it"),
}


@dataclass(frozen=True)
class Release:
    """What a headless-release sweep did: the refusal (a dirty session), the
    paths whose idle headless sessions were dropped, and any close errors."""
    blocked: dict | None = None
    released: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    kind: str = "session"

    def fields(self) -> dict:
        """The extra response keys for the tool that triggered the sweep."""
        if self.kind not in KINDS:
            raise ValueError(f"Release.kind must be one of {sorted(KINDS)}, got {self.kind!r}")
        released_key, errors_key, note = KINDS[self.kind]
        out: dict = {}
        if self.released:
            out[released_key] = list(self.released)
            out["released_note"] = note
        if self.errors:
            out[errors_key] = list(self.errors)
        return out


def close_error_fields(error: str | None) -> dict:
    """A session that did not stop cleanly is reported, never dropped silently."""
    return {"previous_session_close_error": error} if error else {}
