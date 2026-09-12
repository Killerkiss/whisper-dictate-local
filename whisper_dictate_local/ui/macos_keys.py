"""macOS key handling: accelerator parsing, hold detection, global hotkey.

The X11 equivalents of all three live in `gtk_keystate.py` and Keybinder. Here
they are Quartz: `CGEventSourceKeyState` answers "is this key down right now",
and a `CGEventTap` delivers key presses from any application.

Both need **Accessibility** permission (System Settings -> Privacy & Security
-> Accessibility). macOS will not prompt for an unsigned interpreter in a
useful way, so the user has to add the app -- usually the Terminal or Python
binary -- by hand. Everything here degrades to None or False rather than
raising, so the menu bar still runs while that is unresolved and can say so.

UNVERIFIED: written without a Mac to test on. The virtual key codes are the
documented kVK_* constants from HIToolbox's Events.h.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

try:
    import Quartz
except ImportError:  # pragma: no cover - only importable on macOS with PyObjC
    Quartz = None

# kVK_* virtual key codes. Positional on the physical keyboard, not affected by
# the user's layout, which is exactly what a global shortcut wants.
KEYCODES: dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "equal": 24,
    "9": 25, "7": 26, "minus": 27, "8": 28, "0": 29, "bracketright": 30,
    "o": 31, "u": 32, "bracketleft": 33, "i": 34, "p": 35, "l": 37, "j": 38,
    "apostrophe": 39, "k": 40, "semicolon": 41, "backslash": 42, "comma": 43,
    "slash": 44, "n": 45, "m": 46, "period": 47, "grave": 50,
    "return": 36, "tab": 48, "space": 49, "delete": 51, "escape": 53,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
    "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "f13": 105, "f14": 107, "f15": 113,
    "home": 115, "pageup": 116, "end": 119, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
}

# CGEventFlags. Super/Meta maps to Command, which is what a Mac user means by
# it even though GTK's accelerator syntax was written for X11.
MODIFIERS = {
    "shift": 1 << 17,
    "control": 1 << 18,
    "ctrl": 1 << 18,
    "primary": 1 << 20,
    "alt": 1 << 19,
    "option": 1 << 19,
    "meta": 1 << 20,
    "super": 1 << 20,
    "command": 1 << 20,
    "cmd": 1 << 20,
}
_ALL_MODIFIER_BITS = (1 << 17) | (1 << 18) | (1 << 19) | (1 << 20)


def available() -> bool:
    """True when Quartz is importable, i.e. PyObjC is installed."""
    return Quartz is not None


def parse_accelerator(accelerator: str) -> tuple[int, int] | None:
    """"<Super>space" or "F8" -> (keycode, modifier mask), or None.

    The config stores GTK accelerator syntax so one setting means the same
    thing on both platforms; this translates it rather than asking the user to
    learn a second format.
    """
    if not accelerator:
        return None
    mask = 0
    for name in re.findall(r"<([^>]+)>", accelerator):
        bit = MODIFIERS.get(name.strip().lower())
        if bit is None:
            log.warning("unknown modifier %r in %r", name, accelerator)
            return None
        mask |= bit

    key = re.sub(r"<[^>]+>", "", accelerator).strip().lower()
    code = KEYCODES.get(key)
    if code is None:
        log.warning("no macOS key code for %r", key)
        return None
    return code, mask


def is_down(keycode: int) -> bool:
    """Whether that physical key is held right now, whoever has focus."""
    if Quartz is None:
        return False
    try:
        return bool(Quartz.CGEventSourceKeyState(
            Quartz.kCGEventSourceStateHIDSystemState, keycode))
    except Exception as exc:  # noqa: BLE001 - never take the app down for this
        log.warning("CGEventSourceKeyState failed: %s", exc)
        return False


class HotkeyTap:
    """A global hotkey, delivered by a Quartz event tap.

    An event tap rather than Carbon's RegisterEventHotKey because the tap can
    also report the key *release*, which is what push-to-talk needs -- the same
    problem X11 solves by polling XQueryKeymap.
    """

    def __init__(self, accelerator: str, on_press, on_release=None) -> None:
        self.on_press = on_press
        self.on_release = on_release
        self._tap = None
        self._source = None
        self._held = False

        parsed = parse_accelerator(accelerator)
        if parsed is None:
            raise ValueError(f"cannot map {accelerator!r} to a macOS key")
        self.keycode, self.mask = parsed

    def start(self) -> bool:
        """Install the tap. False means macOS refused, almost always because
        Accessibility permission has not been granted."""
        if Quartz is None:
            return False

        mask = Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
        if self.on_release is not None:
            mask |= Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp)

        try:
            self._tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap,
                Quartz.kCGHeadInsertEventTap,
                # Listen-only: the keystroke still reaches the focused app, so
                # binding a key does not steal it from everything else.
                Quartz.kCGEventTapOptionListenOnly,
                mask,
                self._callback,
                None,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not create the event tap: %s", exc)
            return False

        if not self._tap:
            log.warning("event tap refused; grant Accessibility permission")
            return False

        self._source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), self._source,
                                  Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self._tap, True)
        log.info("global hotkey active")
        return True

    def stop(self) -> None:
        if self._tap:
            try:
                Quartz.CGEventTapEnable(self._tap, False)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not disable the event tap: %s", exc)
        self._tap = self._source = None

    def _callback(self, proxy, event_type, event, refcon):
        # macOS disables a tap that takes too long or is interrupted by the
        # screen locking; re-enabling is the documented recovery.
        if event_type in (Quartz.kCGEventTapDisabledByTimeout,
                          Quartz.kCGEventTapDisabledByUserInput):
            log.warning("event tap was disabled by the system; re-enabling")
            Quartz.CGEventTapEnable(self._tap, True)
            return event

        try:
            code = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventKeycode)
            if code == self.keycode:
                flags = Quartz.CGEventGetFlags(event) & _ALL_MODIFIER_BITS
                if flags == self.mask:
                    if event_type == Quartz.kCGEventKeyDown:
                        # Holding a key autorepeats; only the first press counts.
                        if not self._held:
                            self._held = True
                            self.on_press()
                    elif event_type == Quartz.kCGEventKeyUp:
                        self._held = False
                        if self.on_release is not None:
                            self.on_release()
        except Exception:  # noqa: BLE001 - an exception here kills the tap
            log.exception("hotkey callback failed")
        return event
