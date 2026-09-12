#!/usr/bin/env python3
"""Regenerate the settings screenshots used in the README.

Run after changing the settings UI:

    python3 tools/make_screenshots.py

Two things matter here. The dialog is built from `DEFAULTS` rather than the
config on this machine, so a personal vocabulary hint, a real microphone name
or a home directory cannot end up in a public screenshot. And each shot is
taken with a set of tools masked out, which is how the Wayland picture is
produced from an X11 desktop -- capability detection is the only thing that
distinguishes them.

Needs a running X display and ImageMagick's `import`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "screenshots"

# (filename, session, tools to hide, tab index, description)
SHOTS = [
    ("settings-general.png", "x11", (), 0, "everything available"),
    ("settings-audio.png", "x11", (), 1, "everything available"),
    ("settings-wayland.png", "wayland", ("xdotool", "wtype", "ydotool"), 0,
     "no way to type"),
]


def capture(filename: str, session: str, hidden: tuple[str, ...], page: int) -> None:
    """Render one dialog in a child process and screenshot its window.

    A child process because capability detection runs at import time: masking
    tools in-process would leak into the next shot.
    """
    title = f"whisper-dictate-local-shot-{os.getpid()}-{page}"
    script = f"""
import os, shutil, sys
os.environ["XDG_SESSION_TYPE"] = {session!r}
{'os.environ["WAYLAND_DISPLAY"] = "wayland-0"' if session == "wayland"
 else 'os.environ.pop("WAYLAND_DISPLAY", None)'}
_real = shutil.which
shutil.which = lambda n: None if n in {set(hidden)!r} else _real(n)
sys.path.insert(0, {str(ROOT)!r})
from pathlib import Path
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib
from whisper_dictate_local import backend, settings_dialog
from whisper_dictate_local.config import Config, DEFAULTS
backend.refresh()
# Never this machine's real path.
settings_dialog.CONFIG_PATH = Path("~/.config/whisper-dictate-local/config.json")
dlg = settings_dialog.SettingsDialog(Config(dict(DEFAULTS)))
dlg.set_title({title!r})
dlg.show_all()
dlg.get_child().get_children()[0].set_current_page({page})
def snap():
    os.system("import -window {title} " + {str(OUT_DIR / filename)!r})
    Gtk.main_quit()
    return False
GLib.timeout_add(1000, snap)
Gtk.main()
"""
    subprocess.run([sys.executable, "-c", script], check=True)


def main() -> int:
    if not os.environ.get("DISPLAY"):
        print("no DISPLAY; this needs a running X session", file=sys.stderr)
        return 1
    if not shutil.which("import"):
        print("ImageMagick's 'import' is not installed", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for filename, session, hidden, page, why in SHOTS:
        capture(filename, session, hidden, page)
        target = OUT_DIR / filename
        if not target.exists():
            print(f"FAILED {filename}", file=sys.stderr)
            return 1
        print(f"  {filename}  ({session}, {why})  {target.stat().st_size // 1024} KB")
        time.sleep(0.2)
    print(f"written to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
