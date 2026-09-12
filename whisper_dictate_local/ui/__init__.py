"""Front ends, chosen at run time.

`backend.py` decides which command performs a job; this package decides which
toolkit draws the app. The split is the same idea applied one level up: the
tray on Linux and BSD is GTK with an X11 app indicator, and on macOS it is a
menu bar item, but both drive the same `core` pipeline and the same config.

Nothing here is imported until `main()` picks a front end, which matters --
importing the GTK tray on a Mac raises immediately, and importing the macOS
one anywhere else does the same.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)


def available() -> list[str]:
    """Front ends that could plausibly run here, best first."""
    return ["macos"] if sys.platform == "darwin" else ["gtk"]


def main() -> int:
    """Entry point for the tray/menu bar application."""
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    for name in available():
        try:
            if name == "macos":
                from .macos_menubar import main as run
            else:
                from .gtk_tray import main as run
        except ImportError as exc:
            log.error("the %s front end is unavailable: %s", name, exc)
            continue
        return run()

    print(_missing_message(), file=sys.stderr)
    return 1


def _missing_message() -> str:
    if sys.platform == "darwin":
        return (
            "The macOS menu bar app needs rumps and PyObjC:\n"
            "    pip install 'whisper-dictate-local[macos]'\n"
            "Dictation itself works without them -- bind a key to the\n"
            "'whisper-dictate-local' command and skip the menu bar."
        )
    return (
        "The tray needs PyGObject and an app indicator library, which come\n"
        "from your system package manager rather than pip. For example:\n"
        "    sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1 \\\n"
        "                     gir1.2-keybinder-3.0\n"
        "Dictation itself works without them -- bind a key to the\n"
        "'whisper-dictate-local' command and skip the tray."
    )
