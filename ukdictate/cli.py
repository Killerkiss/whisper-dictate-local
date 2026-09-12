"""Headless toggle, invoked by the global keyboard shortcut.

If the tray app is running it just pokes it with SIGUSR1 so there is exactly one
recorder and the panel icon stays truthful. Otherwise it does the whole cycle
itself, so the shortcut still works with no tray.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys

from . import backend
from .config import Config
from .core import (
    STATE_DIR,
    DictationError,
    Recorder,
    deliver,
    has_speech,
    transcribe,
)

log = logging.getLogger(__name__)

TRAY_PIDFILE = STATE_DIR / "tray.pid"
CLI_PIDFILE = STATE_DIR / "cli.pid"


def _notify(message: str, cfg: Config | None = None, level: str = "info") -> None:
    mode = str(cfg["notifications"]) if cfg is not None else "all"
    if mode == "none" or (mode == "errors" and level != "error"):
        return
    backend.notify("Dictation", message)


def _tray_pid() -> int | None:
    try:
        pid = int(TRAY_PIDFILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        TRAY_PIDFILE.unlink(missing_ok=True)  # stale
        return None
    return pid


def _standalone_toggle(cfg: Config) -> int:
    """Record/transcribe without the tray, using a pidfile for the toggle state."""
    recorder = Recorder()

    # Stop branch: a live parecord from the previous invocation.
    if CLI_PIDFILE.exists():
        try:
            pid = int(CLI_PIDFILE.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError):
            pass
        CLI_PIDFILE.unlink(missing_ok=True)

        raw = recorder.raw_path
        if not raw.exists() or raw.stat().st_size == 0:
            _notify("No audio captured", cfg, level="error")
            return 1

        from .core import _raw_to_wav  # local import keeps the public surface small

        _raw_to_wav(raw, recorder.wav_path)

        ok, peak, floor = has_speech(recorder.wav_path, cfg)
        if not ok:
            _notify(f"No speech detected (peak {peak:.0f} dB)", cfg)
            return 0

        _notify("Transcribing...", cfg)
        try:
            text = transcribe(recorder.wav_path, cfg)
        except DictationError as exc:
            _notify(f"Error: {exc}", cfg, level="error")
            return 1

        if not text:
            _notify("Nothing recognized", cfg)
            return 0

        try:
            deliver(text, cfg, None)
        except DictationError as exc:
            # Typing can be unavailable (Wayland); deliver() has already put the
            # text on the clipboard, so this is a warning, not a lost transcript.
            _notify(str(exc), cfg, level="error")
            return 0
        return 0

    # Start branch.
    recorder.start(
        remember_focus=False,
        latency_ms=cfg["capture_latency_ms"],
        device=str(cfg["input_device"]),
        headset_mic=bool(cfg["request_headset_mic"]),
    )
    recorder.wait_until_live()
    if recorder._proc is not None:
        CLI_PIDFILE.write_text(str(recorder._proc.pid), encoding="utf-8")
    _notify("Recording - press the shortcut again to stop", cfg)
    # Detach: parecord must outlive this process so the next press can stop it.
    return 0


def _check(cfg: Config) -> int:
    """Print what this machine can actually do. The first thing to ask for in
    a bug report, since the answer depends on the session, not the distro."""
    for line in backend.TOOLS.describe():
        print(line)

    engine = backend.make_engine(cfg)
    print(f"engine running: {engine.is_running()}")

    if not backend.TOOLS.can_type:
        print("\nTyping is unavailable on this session.")
        if backend.TOOLS.session == "wayland":
            print("Wayland forbids one client typing into another. Either set")
            print("Result to 'Copy' in Settings, or install wtype (wlroots")
            print("compositors) or ydotool (needs /dev/uinput access).")
        else:
            print("Install xdotool.")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="uk-dictate",
        description="Toggle dictation. Bind this to a keyboard shortcut.",
    )
    parser.add_argument("--no-tray", action="store_true",
                        help="ignore a running tray app and record standalone")
    parser.add_argument("--check", action="store_true",
                        help="report which backend was detected, then exit")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)
    cfg = Config.load()

    if args.check:
        return _check(cfg)

    if not args.no_tray:
        pid = _tray_pid()
        if pid is not None:
            os.kill(pid, signal.SIGUSR1)
            return 0

    return _standalone_toggle(cfg)


if __name__ == "__main__":
    sys.exit(main())
