#!/usr/bin/env python3
"""No modal dialogs from a KiCad-python child process (ROADMAP_DECISIONS D0).

KiCad's bundled Python loads wxWidgets together with pcbnew. A failed wx
assertion (``SHAPE_POLY_SET::SquaredDistance(p, True)`` on 9.0) or a
wxLogError (``can't open file`` from an Eagle ``FootprintEnumerate`` of a
missing ``.lbr``) then opens a MODAL dialog. Nobody can dismiss it in a
headless child, so the process blocks forever. Importing this module first
turns wx asserts off and sends wx log messages from Info level up to stderr,
where the caller already captures them::

    import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
    import pcbnew

Every server/tools script that imports pcbnew does this, and
tests/test_kicad_quiet.py enforces it. A script run as a file has
server/tools on ``sys.path[0]``. Inline ``-c`` code must put it there first.
Every KiCad child also gets ``stdin=DEVNULL`` (toolchain.run).

The module uses only the stdlib and wx, because KiCad 9's bundled Python
has no third-party packages. Without wx (a KiCad Python built without
wxPython) it is a silent no-op, since there is no wx GUI to raise a dialog.
After the first success, importing it again, calling ``quiet()`` again or
reloading it changes nothing. If wx is importable but cannot be silenced,
the import raises RuntimeError rather than let pcbnew run with dialogs on.
"""
from __future__ import annotations

# The wx.LogStderr installed as the active log target. It stays referenced for
# the life of the process because wx keeps only a raw C++ pointer to it: if the
# Python wrapper were collected, wx would log through a dangling pointer.
# globals().get() keeps the SAME object across importlib.reload().
_TARGET = globals().get("_TARGET")
# The target it replaced. wx hands that one back to the caller to delete; it is
# kept instead, because code loaded earlier may still point at it.
_PREVIOUS = globals().get("_PREVIOUS")


def quiet() -> bool:
    """Turn wx asserts off and route wx log messages (Info and up) to stderr, once.

    Returns True when wx is configured (now or by an earlier call) and False
    when wx is not importable (nothing to silence). Raises RuntimeError when
    wx imports but the calls fail.
    """
    global _TARGET, _PREVIOUS
    if _TARGET is not None:
        return True
    try:
        import wx
    except ImportError:
        return False
    try:
        wx.DisableAsserts()
        # Errors, warnings and messages still reach stderr. wxLogDebug chatter
        # does not: the first LoadBoard alone logs a dozen "Adding duplicate
        # image handler" lines that the default target never showed.
        wx.Log.SetLogLevel(wx.LOG_Info)
        target = wx.LogStderr()
        previous = wx.Log.SetActiveTarget(target)
    except Exception as e:  # an old or partial wxPython: refuse, never risk a dialog
        raise RuntimeError(
            f"kicad_quiet: wx is importable but its asserts / log dialogs could not be "
            f"turned off ({type(e).__name__}: {e}); refusing to continue, because a "
            f"modal wx dialog in a headless KiCad child blocks forever") from e
    _TARGET, _PREVIOUS = target, previous
    return True


def log_target() -> object | None:
    """The wx.LogStderr that quiet() installed and keeps alive (None without wx)."""
    return _TARGET


# Configure wx on import: importing this module before pcbnew is the contract.
ACTIVE: bool = quiet()
