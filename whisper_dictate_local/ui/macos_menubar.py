"""macOS menu bar front end.

The counterpart to `gtk_tray.py`: same pipeline, same config file, same
SIGUSR1 contract with the CLI, drawn as a menu bar item instead of a panel
indicator.

Two deliberate differences from the GTK tray:

* **Settings are menu items, not a dialog.** The options people change often --
  language, translate, where the text goes -- are toggles in the menu, and
  everything else opens the JSON in the default editor. A Cocoa form would
  duplicate 350 lines of GTK dialog for settings that are mostly set once.
* **The global hotkey is optional.** It needs Accessibility permission, which
  macOS will not grant an unsigned interpreter without the user going into
  System Settings. When it is unavailable the app says so and still works --
  bind a key to the `whisper-dictate-local` command in Raycast, Hammerspoon,
  Karabiner or an Automator Quick Action, exactly as Wayland users bind theirs
  in the compositor. The CLI signals this app, so behaviour is identical.

UNVERIFIED: written without a Mac to test on.
"""

from __future__ import annotations

import logging
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path

import rumps

from .. import backend, sounds
from ..backend import STATE_DIR
from ..config import CONFIG_PATH, Config
from ..core import (
    DictationError,
    Recorder,
    deliver,
    duration_s,
    has_speech,
    transcribe,
)
from . import macos_keys
from .options import LANGUAGES, language_label, output_modes

log = logging.getLogger(__name__)

PIDFILE = STATE_DIR / "tray.pid"

# The menu bar has no equivalent of a themed icon, and a template PNG would
# have to be generated and shipped for every state. Emoji read correctly in
# both light and dark menu bars and need no assets.
TITLES = {"idle": "🎙", "recording": "🔴", "busy": "•••"}

# How often the main thread wakes to apply queued work. Python signal handlers
# only run when the interpreter executes bytecode, and the Cocoa run loop does
# not; this timer is what lets SIGUSR1 from the CLI ever be noticed.
TICK_S = 0.1


class MenuBarApp(rumps.App):
    def __init__(self) -> None:
        super().__init__(TITLES["idle"], quit_button=None)
        self.cfg = Config.load()
        self.recorder = Recorder()
        self.engine = backend.make_engine(self.cfg)
        self.busy = False
        self._hotkey: macos_keys.HotkeyTap | None = None
        self._last_use = 0.0
        self._engine_started_by_us = False
        self._record_deadline = 0.0
        # Work handed back from the transcription thread. Cocoa is not thread
        # safe, so nothing but the main thread touches the menu.
        self._main_thread_work: queue.Queue = queue.Queue()

        log.info("backend -- %s", "; ".join(backend.TOOLS.describe()))
        sounds.ensure(self.cfg["sound_lead_in_ms"])
        self._build_menu()
        self._write_pidfile()

        signal.signal(signal.SIGUSR1, self._on_signal)
        rumps.Timer(self._tick, TICK_S).start()
        rumps.Timer(self._check_idle, 60).start()
        self._bind_hotkey()

    # -- menu ---------------------------------------------------------------
    def _build_menu(self) -> None:
        self.item_toggle = rumps.MenuItem("Start dictation", callback=self.on_toggle)
        self.item_status = rumps.MenuItem("Ready")
        self.item_status.set_callback(None)  # a label, not a button
        self.item_hotkey = rumps.MenuItem("", callback=None)

        lang_items = []
        for code, label in LANGUAGES:
            item = rumps.MenuItem(f"{label}  ({code})", callback=self.on_language)
            item.language_code = code
            lang_items.append(item)

        mode_items = []
        for code, label in output_modes():
            item = rumps.MenuItem(label, callback=self.on_output_mode)
            item.mode_code = code
            mode_items.append(item)
        # Nothing selectable means no way to deliver text at all; say so rather
        # than showing an empty submenu.
        if not mode_items:
            disabled = rumps.MenuItem("No way to type or copy was found")
            disabled.set_callback(None)
            mode_items = [disabled]

        self.item_translate = rumps.MenuItem("Translate to English",
                                             callback=self.on_translate)
        self.item_engine = rumps.MenuItem("Speech engine", callback=self.on_engine)

        self.menu = [
            self.item_toggle,
            self.item_status,
            self.item_hotkey,
            None,
            ["Spoken language", lang_items],
            ["Result", mode_items],
            self.item_translate,
            None,
            self.item_engine,
            None,
            rumps.MenuItem("Edit settings file…", callback=self.on_edit_config),
            rumps.MenuItem("Reload settings", callback=self.on_reload),
            None,
            rumps.MenuItem("Quit", callback=self.on_quit),
        ]
        self._refresh_menu()

    def _refresh_menu(self) -> None:
        """Make the menu agree with the config. Called after every change."""
        self.item_translate.state = 1 if self.cfg["translate"] else 0

        lang = str(self.cfg["language"])
        for item in self.menu["Spoken language"].values():
            item.state = 1 if getattr(item, "language_code", None) == lang else 0

        mode = str(self.cfg["output_mode"])
        for item in self.menu["Result"].values():
            item.state = 1 if getattr(item, "mode_code", None) == mode else 0

        running = self.engine.is_running()
        self.item_engine.title = (f"Speech engine: {'running' if running else 'stopped'}"
                                  f" (click to {'stop' if running else 'start'})")

    # -- hotkey -------------------------------------------------------------
    def _bind_hotkey(self) -> None:
        if self._hotkey is not None:
            self._hotkey.stop()
            self._hotkey = None

        accelerator = str(self.cfg["hotkey"])
        if not macos_keys.available():
            self.item_hotkey.title = "Shortcut: needs PyObjC — bind the CLI instead"
            return
        try:
            hold = str(self.cfg.get("hotkey_mode", "toggle")) == "hold"
            tap = macos_keys.HotkeyTap(
                accelerator,
                on_press=self._on_hotkey_press,
                on_release=self._on_hotkey_release if hold else None,
            )
        except ValueError as exc:
            self.item_hotkey.title = f"Shortcut: {exc}"
            log.warning("%s", exc)
            return

        if tap.start():
            self._hotkey = tap
            self.item_hotkey.title = f"Shortcut: {accelerator}"
        else:
            # The overwhelmingly common cause, and the fix is not obvious.
            self.item_hotkey.title = "Shortcut: needs Accessibility permission"
            log.warning(
                "the global shortcut needs Accessibility permission: System "
                "Settings -> Privacy & Security -> Accessibility. Until then, "
                "bind a key to the whisper-dictate-local command instead.")

    def _on_hotkey_press(self) -> None:
        # Runs on the run loop; queue it so it is handled like everything else.
        self._main_thread_work.put(
            self._start if str(self.cfg.get("hotkey_mode")) == "hold" else self.toggle)

    def _on_hotkey_release(self) -> None:
        def release() -> None:
            if self.recorder.active and not self.busy:
                self._stop_and_transcribe()
        self._main_thread_work.put(release)

    # -- the CLI pokes us rather than starting a second recorder -------------
    def _on_signal(self, _signum, _frame) -> None:
        self._main_thread_work.put(self.toggle)

    def _write_pidfile(self) -> None:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
        except OSError as exc:
            log.warning("could not write %s: %s", PIDFILE, exc)

    # -- main loop ----------------------------------------------------------
    def _tick(self, _timer) -> None:
        """Drain work queued from other threads and the event tap."""
        while True:
            try:
                job = self._main_thread_work.get_nowait()
            except queue.Empty:
                break
            try:
                job()
            except Exception:  # noqa: BLE001 - one bad job must not stop the app
                log.exception("queued job failed")

        # Safety net: never let a forgotten session record forever.
        if (self._record_deadline and self.recorder.active and not self.busy
                and time.monotonic() > self._record_deadline):
            self._record_deadline = 0.0
            self._notify("Recording stopped (time limit)")
            self._stop_and_transcribe()

    # -- dictation ----------------------------------------------------------
    def on_toggle(self, _sender=None) -> None:
        self.toggle()

    def toggle(self) -> None:
        if self.busy:
            return
        if self.recorder.active:
            self._stop_and_transcribe()
        else:
            self._start()

    def _start(self) -> None:
        if self.busy or self.recorder.active:
            return
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
        self._record_deadline = time.monotonic() + int(self.cfg["max_recording_s"])

    def _stop_and_transcribe(self) -> None:
        self._record_deadline = 0.0
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
        threading.Thread(target=self._worker, args=(wav, window),
                         daemon=True, name="transcribe").start()

    def _worker(self, wav: Path, window: str | None) -> None:
        """Off the main thread; results go back through the queue."""
        def done(fn, *args):
            self._main_thread_work.put(lambda: fn(*args))
        try:
            if self.cfg["manage_engine"]:
                self._start_engine()
            self._last_use = time.monotonic()

            ok, peak, floor = has_speech(wav, self.cfg)
            if not ok:
                done(self._finish_quiet, peak, floor)
                return

            text = transcribe(wav, self.cfg)
            if not text:
                done(self._finish, "Nothing recognized")
                return

            deliver(text, self.cfg, window)
            done(self._finish_ok, text, duration_s(wav))
        except DictationError as exc:
            done(self._finish_error, str(exc))
        except Exception as exc:  # noqa: BLE001 - surface anything unexpected
            log.exception("transcription failed")
            done(self._finish_error, str(exc))

    def _finish(self, message: str) -> None:
        self.busy = False
        self._set_state("idle")
        self._notify(message)

    def _finish_quiet(self, peak: float, floor: float) -> None:
        self._finish(f"No speech detected (peak {peak:.0f} dB, floor {floor:.0f} dB)")

    def _finish_ok(self, text: str, secs: float) -> None:
        self.busy = False
        self._set_state("idle")
        preview = text if len(text) <= 60 else text[:57] + "..."
        self.item_status.title = f"Last: {preview}"
        self._notify(preview, title=f"Transcribed {secs:.0f}s")

    def _finish_error(self, message: str) -> None:
        self.busy = False
        self._set_state("idle")
        self._notify(f"Error: {message}", level="error")

    # -- menu actions -------------------------------------------------------
    def on_language(self, sender) -> None:
        self.cfg["language"] = sender.language_code
        self.cfg.save()
        self._refresh_menu()
        self._notify(f"Language: {language_label(sender.language_code)}")

    def on_output_mode(self, sender) -> None:
        self.cfg["output_mode"] = sender.mode_code
        self.cfg.save()
        self._refresh_menu()

    def on_translate(self, _sender) -> None:
        self.cfg["translate"] = not self.cfg["translate"]
        self.cfg.save()
        self._refresh_menu()
        self._notify("Translating to English" if self.cfg["translate"]
                     else "Transcribing in the spoken language")

    def on_engine(self, _sender) -> None:
        if self.engine.is_running():
            self.engine.stop()
        else:
            self.engine.start()
        self._engine_started_by_us = False
        self._refresh_menu()

    def on_edit_config(self, _sender) -> None:
        self.cfg.save()  # make sure the file exists before opening it
        subprocess.run(["open", "-t", str(CONFIG_PATH)], check=False)

    def on_reload(self, _sender) -> None:
        self.cfg = Config.load()
        sounds.ensure(self.cfg["sound_lead_in_ms"])
        self._bind_hotkey()
        self._refresh_menu()
        self._notify("Settings reloaded")

    def on_quit(self, _sender) -> None:
        if self.cfg["manage_engine"] and self._engine_started_by_us:
            self.engine.stop()
        if self._hotkey is not None:
            self._hotkey.stop()
        self.recorder.cancel()
        PIDFILE.unlink(missing_ok=True)
        rumps.quit_application()

    # -- engine -------------------------------------------------------------
    def _start_engine(self) -> None:
        if self.engine.is_running():
            return
        started, message = self.engine.start()
        if started:
            self._engine_started_by_us = True
            log.info("%s", message)
        else:
            log.warning("could not start speech engine: %s", message)

    def _check_idle(self, _timer) -> None:
        """Release the model once it has gone unused."""
        timeout_min = float(self.cfg.get("engine_idle_timeout_min", 15) or 0)
        if (timeout_min > 0 and self.cfg["manage_engine"]
                and self._engine_started_by_us and not self.recorder.active
                and not self.busy and self._last_use
                and (time.monotonic() - self._last_use) > timeout_min * 60):
            if self.engine.is_running():
                self.engine.stop()
                log.info("speech engine stopped after %g min idle", timeout_min)
            self._engine_started_by_us = False
            self._last_use = 0.0
        self._refresh_menu()

    # -- feedback -----------------------------------------------------------
    def _set_state(self, state: str) -> None:
        self.title = TITLES.get(state, TITLES["idle"])

    def _sound(self, event: str) -> None:
        if not self.cfg["play_sounds"]:
            return
        path = sounds.CACHE_DIR / f"{event}.wav"
        if path.exists():
            backend.play(path)

    def _notify(self, message: str, title: str = "Dictation",
                level: str = "info") -> None:
        mode = str(self.cfg["notifications"])
        if mode == "none" or (mode == "errors" and level != "error"):
            return
        try:
            rumps.notification(title, "", message)
        except Exception as exc:  # noqa: BLE001 - unsigned builds cannot notify
            log.info("notification suppressed (%s): %s", exc, message)


def main() -> int:
    MenuBarApp().run()
    return 0
