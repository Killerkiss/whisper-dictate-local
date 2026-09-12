"""Tell whether the hotkey is still physically held down.

Keybinder delivers a press but never a release, so push-to-talk needs the key
state read directly from X. `XQueryKeymap` returns a bitmap of every key
currently down, which works regardless of who holds the grab or which window
has focus.
"""

from __future__ import annotations

import logging

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

log = logging.getLogger(__name__)

try:
    from Xlib import display as _xdisplay

    _DISPLAY = _xdisplay.Display()
except Exception as exc:  # noqa: BLE001 - any Xlib/display failure disables hold mode
    log.warning("Xlib unavailable (%s); push-to-talk will fall back to toggle", exc)
    _DISPLAY = None


def available() -> bool:
    return _DISPLAY is not None


def keycodes_for(accelerator: str) -> list[int]:
    """Map a GTK accelerator such as "F8" or "<Super>space" to X keycodes."""
    try:
        keyval, _mods = Gtk.accelerator_parse(accelerator)
    except (TypeError, ValueError):
        return []
    if not keyval:
        return []

    display = Gdk.Display.get_default()
    if display is None:
        return []
    keymap = Gdk.Keymap.get_for_display(display)
    found, entries = keymap.get_entries_for_keyval(keyval)
    if not found:
        return []
    # A keyval can sit on several physical keys; any of them counts as held.
    return sorted({int(e.keycode) for e in entries})


def any_down(keycodes: list[int]) -> bool:
    """True while at least one of these keycodes is physically held."""
    if _DISPLAY is None or not keycodes:
        return False
    try:
        bitmap = _DISPLAY.query_keymap()
    except Exception as exc:  # noqa: BLE001 - a dropped connection must not crash the tray
        log.warning("query_keymap failed: %s", exc)
        return False

    for code in keycodes:
        if 0 <= code < 256 and bitmap[code // 8] & (1 << (code % 8)):
            return True
    return False
