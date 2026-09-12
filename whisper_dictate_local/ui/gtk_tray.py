"""Panel indicator: shows dictation state and hosts the settings UI."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Notify", "0.7")
gi.require_version("Keybinder", "3.0")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except (ValueError, ImportError):  # pragma: no cover - depends on distro packaging
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3 as AppIndicator

from gi.repository import GLib, Gtk, Keybinder, Notify  # noqa: E402

from .. import backend, sounds  # noqa: E402
from . import gtk_keystate as keystate  # noqa: E402
from ..config import Config  # noqa: E402
from ..core import (  # noqa: E402
    STATE_DIR,
    DictationError,
    Recorder,
    deliver,
    duration_s,
    has_speech,
    transcribe,
)
from .gtk_settings import SettingsDialog  # noqa: E402

log = logging.getLogger(__name__)

APP_ID = "whisper-dictate-local"
# How often push-to-talk checks whether the key is still down. 40ms is well
# below human reaction time and costs nothing measurable.
POLL_RELEASE_MS = 40
PIDFILE = STATE_DIR / "tray.pid"

# Installed into ~/.local/share/icons/hicolor/scalable/apps by install.sh.
# The recording/busy icons are full-colour on purpose: a "-symbolic" icon gets
# recoloured to the panel foreground, which would throw away the red.
ICON_IDLE = "whisper-dictate-local-idle-symbolic"
ICON_RECORDING = "whisper-dictate-local-recording"
ICON_BUSY = "whisper-dictate-local-busy"

# Fall back to stock names if the themed icons are not installed.
ICON_FALLBACK = {
    ICON_IDLE: "audio-input-microphone-symbolic",
    ICON_RECORDING: "media-record",
    ICON_BUSY: "content-loading-symbolic",
}


class TrayApp:
    def __init__(self) -> None:
        self.cfg = Config.load()
        self.recorder = Recorder()
        self.engine = backend.make_engine(self.cfg)
        self.busy = False
        log.info("backend -- %s", "; ".join(backend.TOOLS.describe()))

        Notify.init("Dictation")
        Keybinder.init()
        self._bound_key: str | None = None

        self.indicator = AppIndicator.Indicator.new(
            APP_ID, ICON_IDLE, AppIndicator.IndicatorCategory.APPLICATION_STATUS
        )
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_title("Dictation")

        self.menu = Gtk.Menu()
        self.item_toggle = Gtk.MenuItem(label="Start dictation")
        self.item_toggle.connect("activate", lambda *_: self.toggle())
        self.menu.append(self.item_toggle)

        self.item_status = Gtk.MenuItem(label="Idle")
        self.item_status.set_sensitive(False)
        self.menu.append(self.item_status)

        self.menu.append(Gtk.SeparatorMenuItem())

        item_settings = Gtk.MenuItem(label="Settings...")
        item_settings.connect("activate", self.open_settings)
        self.menu.append(item_settings)

        item_shortcut = Gtk.MenuItem(label="Keyboard shortcut...")
        item_shortcut.connect("activate", lambda *_: _spawn(["cinnamon-settings", "keyboard"]))
        self.menu.append(item_shortcut)

        self.item_server = Gtk.MenuItem(label="Speech engine: checking...")
        self.item_server.connect("activate", lambda *_: self.toggle_server())
        self.menu.append(self.item_server)

        self.menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", lambda *_: self.quit())
        self.menu.append(item_quit)

        self.menu.show_all()
        self.indicator.set_menu(self.menu)
        # Middle-click the panel icon to toggle without opening the menu.
        self.indicator.set_secondary_activate_target(self.item_toggle)

        self._bind_hotkey()
        sounds.ensure(self.cfg["sound_lead_in_ms"])
        self._write_pidfile()
        self._engine_started_by_us = False
        self._last_use = 0.0
        # The engine is started lazily on first dictation; an idle timer shuts
        # it down again so VRAM is only held while actually in use.
        GLib.timeout_add_seconds(60, self._check_idle)
        # The global hotkey delivers SIGUSR1 rather than starting a second copy.
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, self._on_signal)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda *_: self.quit())
        # Ctrl-C when run from a terminal. Without it the interpreter dies on
        # KeyboardInterrupt and parecord, being a child process rather than a
        # thread, outlives it holding the microphone open.
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda *_: self.quit())
        # SIGHUP re-reads the config file, so edits made outside the settings
        # dialog take effect without restarting the tray.
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGHUP, self._on_reload)
        GLib.timeout_add_seconds(10, self._refresh_server_label)
        GLib.idle_add(self._refresh_server_label)

    # -- lifecycle ----------------------------------------------------------
    def _write_pidfile(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        PIDFILE.write_text(str(os.getpid()), encoding="utf-8")

    def _on_signal(self, *_args) -> bool:
        self.toggle()
        return True  # keep the handler installed

    def _on_reload(self, *_args) -> bool:
        self._reload_config()
        self._notify("Settings reloaded")
        return True

    # -- global hotkey ------------------------------------------------------
    def _bind_hotkey(self) -> None:
        """Grab the shortcut ourselves so it works regardless of the DE."""
        key = str(self.cfg["hotkey"]).strip()
        if self._bound_key == key:
            return
        if self._bound_key:
            try:
                Keybinder.unbind(self._bound_key)
            except (GLib.Error, RuntimeError):
                pass
            self._bound_key = None
        if not key:
            return
        if Keybinder.bind(key, lambda *_a: self._on_hotkey()):
            self._bound_key = key
            log.info("hotkey bound: %s", key)
        else:
            # Another application already holds this grab.
            log.warning("could not bind hotkey %s", key)
            self._notify(f"Could not grab {key} - it may be taken by another app", level="error")

    def quit(self) -> None:
        # Only stop what we started, so a manually started engine survives.
        if self.cfg["manage_engine"] and self._engine_started_by_us:
            self.engine.stop()
            log.info("speech engine stopped")
        if self._bound_key:
            try:
                Keybinder.unbind(self._bound_key)
            except (GLib.Error, RuntimeError):
                pass
        self.recorder.cancel()
        PIDFILE.unlink(missing_ok=True)
        Gtk.main_quit()

    # -- dictation ----------------------------------------------------------
    def toggle(self) -> None:
        if self.busy:
            return
        if self.recorder.active:
            self._stop_and_transcribe()
        else:
            self._start()

    def _start(self) -> None:
        try:
            self.recorder.start(
                remember_focus=self.cfg["restore_focus"],
                latency_ms=self.cfg["capture_latency_ms"],
                device=str(self.cfg["input_device"]),
                headset_mic=bool(self.cfg["request_headset_mic"]),
                pause_media=bool(self.cfg["pause_media_while_recording"]),
            )
        except DictationError as exc:
            self._notify(f"Error: {exc}", level="error")
            return

        self.recorder.wait_until_live()
        self._set_state("recording")
        self._sound("start")
        self._notify("Recording - speak now")

        # Safety net: never let a forgotten session record forever.
        GLib.timeout_add_seconds(int(self.cfg["max_recording_s"]), self._auto_stop)

    def _auto_stop(self) -> bool:
        if self.recorder.active and not self.busy:
            self._notify("Recording stopped (time limit)")
            self._stop_and_transcribe()
        return False

    def _stop_and_transcribe(self) -> None:
        try:
            wav = self.recorder.stop()
        except DictationError as exc:
            self._set_state("idle")
            self._notify(f"Error: {exc}", level="error")
            return

        window = self.recorder.source_window
        self.busy = True
        self._set_state("busy")
        self._sound("stop")

        threading.Thread(
            target=self._worker, args=(wav, window), daemon=True, name="transcribe"
        ).start()

    def _worker(self, wav: Path, window: str | None) -> None:
        """Runs off the GTK thread; all UI updates go back via idle_add."""
        try:
            if self.cfg["manage_engine"]:
                self._start_engine()
            self._last_use = time.monotonic()

            ok, peak, floor = has_speech(wav, self.cfg)
            if not ok:
                GLib.idle_add(self._finish_quiet, peak, floor)
                return

            text = transcribe(wav, self.cfg)
            if not text:
                GLib.idle_add(self._finish_empty)
                return

            deliver(text, self.cfg, window)
            GLib.idle_add(self._finish_ok, text, duration_s(wav))
        except DictationError as exc:
            GLib.idle_add(self._finish_error, str(exc))
        except Exception as exc:  # noqa: BLE001 - surface anything unexpected
            log.exception("transcription failed")
            GLib.idle_add(self._finish_error, str(exc))

    def _finish_quiet(self, peak: float, floor: float) -> bool:
        self.busy = False
        self._set_state("idle")
        self._notify(
            f"No speech detected (peak {peak:.0f} dB, floor {floor:.0f} dB)"
        )
        return False

    def _finish_empty(self) -> bool:
        self.busy = False
        self._set_state("idle")
        self._notify("Nothing recognized")
        return False

    def _finish_ok(self, text: str, secs: float) -> bool:
        self.busy = False
        self._set_state("idle")
        preview = text if len(text) <= 60 else text[:57] + "..."
        self.item_status.set_label(f"Last: {preview}")
        self._notify(preview, title=f"Transcribed {secs:.0f}s")
        return False

    def _finish_error(self, message: str) -> bool:
        self.busy = False
        self._set_state("idle")
        self._notify(f"Error: {message}", level="error")
        return False

    # -- ui helpers ---------------------------------------------------------
    def _icon(self, name: str) -> str:
        theme = Gtk.IconTheme.get_default()
        if theme.has_icon(name):
            return name
        return ICON_FALLBACK.get(name, name)

    def _set_state(self, state: str) -> None:
        if state == "recording":
            # ATTENTION status makes the panel show the attention icon, so it
            # must be set to the red one too or the icon silently reverts.
            self.indicator.set_attention_icon_full(self._icon(ICON_RECORDING), "Recording")
            self.indicator.set_icon_full(self._icon(ICON_RECORDING), "Recording")
            self.indicator.set_status(AppIndicator.IndicatorStatus.ATTENTION)
            self.item_toggle.set_label("Stop and transcribe")
            self.item_status.set_label("Recording...")
        elif state == "busy":
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            self.indicator.set_icon_full(self._icon(ICON_BUSY), "Transcribing")
            self.item_toggle.set_label("Working...")
            self.item_status.set_label("Transcribing...")
        else:
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            self.indicator.set_icon_full(self._icon(ICON_IDLE), "Idle")
            self.item_toggle.set_label("Start dictation")

    def _notify(self, body: str, title: str = "Dictation", level: str = "info") -> None:
        mode = str(self.cfg["notifications"])
        if mode == "none" or (mode == "errors" and level != "error"):
            return
        try:
            note = Notify.Notification.new(title, body, "audio-input-microphone")
            note.set_timeout(2500)
            note.show()
        except GLib.Error as exc:
            log.warning("notification failed: %s", exc)

    def _sound(self, event: str) -> None:
        """Play a short cue.

        Deliberately does not use canberra: it honours the desktop's global
        "event sounds" switch, so with that off the app would be silent even
        with sounds enabled here. Playing the file directly keeps this app's
        cues independent of that system-wide setting.
        """
        if not self.cfg["play_sounds"]:
            return

        path = _sound_file(event)
        if path is None:
            log.warning("no sound file found for %r", event)
            return

        backend.play(path)

    def open_settings(self, *_args) -> None:
        dialog = SettingsDialog(self.cfg, on_saved=self._reload_config)
        dialog.show_all()

    def _reload_config(self) -> None:
        self.cfg = Config.load()
        self._bind_hotkey()
        sounds.ensure(self.cfg["sound_lead_in_ms"])

    # -- engine service -----------------------------------------------------
    def _start_engine(self) -> None:
        """Bring the engine up on demand. Safe to call when already running."""
        if self._server_running():
            return  # already up; leave ownership with whoever started it
        started, message = self.engine.start()
        if started:
            self._engine_started_by_us = True
            log.info("%s", message)
        else:
            log.warning("could not start speech engine: %s", message)
        GLib.idle_add(self._refresh_server_label)

    def _on_hotkey(self) -> None:
        """Route the shortcut according to the configured behaviour."""
        if str(self.cfg.get("hotkey_mode", "toggle")) != "hold":
            self.toggle()
            return

        if not keystate.available():
            log.warning("push-to-talk needs python3-xlib; using toggle instead")
            self.toggle()
            return

        codes = keystate.keycodes_for(str(self.cfg["hotkey"]))
        if not codes:
            log.warning("could not map %s to a keycode; using toggle instead",
                        self.cfg["hotkey"])
            self.toggle()
            return

        # Key autorepeat fires this repeatedly while held, so ignore re-entry.
        if self.busy or self.recorder.active:
            return

        self._start()
        GLib.timeout_add(POLL_RELEASE_MS, self._poll_release, codes)

    def _poll_release(self, codes: list[int]) -> bool:
        """While in hold mode, stop as soon as the key comes back up."""
        if not self.recorder.active:
            return False  # already stopped some other way
        if keystate.any_down(codes):
            return True  # still held, keep polling
        self._stop_and_transcribe()
        return False

    def _check_idle(self) -> bool:
        """Shut the engine down once it has gone unused, reclaiming VRAM."""
        timeout_min = float(self.cfg.get("engine_idle_timeout_min", 15) or 0)
        if (
            timeout_min > 0
            and self.cfg["manage_engine"]
            and self._engine_started_by_us
            and not self.recorder.active
            and not self.busy
            and self._last_use
            and (time.monotonic() - self._last_use) > timeout_min * 60
        ):
            if self._server_running():
                self.engine.stop()
                log.info("speech engine stopped after %g min idle", timeout_min)
            self._engine_started_by_us = False
            self._last_use = 0.0
            self._refresh_server_label()
        return True  # keep the timer running

    def _server_running(self) -> bool:
        return self.engine.is_running()

    def _refresh_server_label(self) -> bool:
        running = self._server_running()
        self.item_server.set_label(
            "Speech engine: running (click to stop)" if running
            else "Speech engine: stopped (click to start)"
        )
        return True

    def toggle_server(self) -> None:
        running = self._server_running()
        action = "stop" if running else "start"
        if running:
            self.engine.stop()
        else:
            self.engine.start()
        # Starting by hand hands ownership back to the user; stopping by hand
        # means there is nothing left for us to clean up on quit.
        self._engine_started_by_us = False
        GLib.timeout_add_seconds(1, self._refresh_server_label)
        self._notify(f"Speech engine {action}ed")


# The app's own cues come first: they open with ~350ms of silence so a
# Bluetooth sink that has gone idle can wake up without swallowing the tone.
# The stock system sounds are too short to survive that and are only a fallback.
# Generated cues come first; they carry the configured lead-in silence. The
# stock system sounds are a fallback only - they are too short to survive a
# Bluetooth sink waking from idle.
SOUND_DIRS = (
    sounds.CACHE_DIR,
    Path("/usr/share/sounds/freedesktop/stereo"),
    Path("/usr/share/sounds/gnome/default/alerts"),
)

# Start and stop must sound different, or the cue tells you nothing about which
# edge you just hit.
SOUND_CANDIDATES = {
    "start": ("start.wav", "message.oga", "bell.oga"),
    "stop": ("stop.wav", "complete.oga", "bell.oga"),
    "error": ("error.wav", "dialog-error.oga", "bell.oga"),
}


def _sound_file(event: str) -> Path | None:
    for filename in SOUND_CANDIDATES.get(event, ()):
        for directory in SOUND_DIRS:
            candidate = directory / filename
            if candidate.exists():
                return candidate
    return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    TrayApp()
    Gtk.main()
    return 0
