"""GTK settings window.

Every value in the config file is editable here; nothing requires hand-editing
JSON. Grouped into tabs because a single column got unreadable once the audio
and engine knobs were added.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from .config import CONFIG_PATH, Config  # noqa: E402


def _hold_supported() -> bool:
    try:
        from . import keystate

        return keystate.available()
    except Exception:  # noqa: BLE001 - the dialog must open regardless
        return False

LANGUAGES = [
    ("auto", "Auto-detect"),
    ("uk", "Ukrainian"),
    ("en", "English"),
    ("pl", "Polish"),
    ("de", "German"),
    ("es", "Spanish"),
    ("fr", "French"),
    ("ru", "Russian"),
]

OUTPUT_MODES = [
    ("type_and_copy", "Type it and copy to clipboard"),
    ("type", "Type it into the focused window"),
    ("copy", "Copy to clipboard only"),
]


class _Page(Gtk.Grid):
    """One tab: label / widget / hint rows."""

    def __init__(self) -> None:
        super().__init__(row_spacing=8, column_spacing=14)
        self.set_border_width(18)
        self._row = 0

    def add(self, label: str, widget: Gtk.Widget, hint: str = "") -> Gtk.Widget:
        lbl = Gtk.Label(label=label, xalign=0)
        lbl.set_valign(Gtk.Align.START)
        self.attach(lbl, 0, self._row, 1, 1)
        self.attach(widget, 1, self._row, 1, 1)
        self._row += 1
        if hint:
            h = Gtk.Label(label=hint, xalign=0, wrap=True)
            h.set_max_width_chars(50)
            h.get_style_context().add_class("dim-label")
            self.attach(h, 1, self._row, 1, 1)
            self._row += 1
        return widget

    def add_wide(self, widget: Gtk.Widget) -> Gtk.Widget:
        self.attach(widget, 0, self._row, 2, 1)
        self._row += 1
        return widget


def _spin(value: float, lo: float, hi: float, step: float = 1, digits: int = 0) -> Gtk.SpinButton:
    adj = Gtk.Adjustment(value=float(value), lower=lo, upper=hi,
                         step_increment=step, page_increment=step * 5)
    sb = Gtk.SpinButton(adjustment=adj, climb_rate=1, digits=digits)
    sb.set_halign(Gtk.Align.START)
    return sb


def _entry(text: str, placeholder: str = "") -> Gtk.Entry:
    e = Gtk.Entry()
    e.set_text(str(text or ""))
    e.set_hexpand(True)
    if placeholder:
        e.set_placeholder_text(placeholder)
    return e


class SettingsDialog(Gtk.Window):
    def __init__(self, cfg: Config, on_saved=None) -> None:
        super().__init__(title="Dictation Settings")
        self.cfg = cfg
        self.on_saved = on_saved

        self.set_default_size(560, 520)
        self.set_position(Gtk.WindowPosition.CENTER)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(outer)

        notebook = Gtk.Notebook()
        notebook.set_border_width(8)
        outer.pack_start(notebook, True, True, 0)

        notebook.append_page(self._page_general(), Gtk.Label(label="General"))
        notebook.append_page(self._page_audio(), Gtk.Label(label="Audio"))
        notebook.append_page(self._page_engine(), Gtk.Label(label="Engine"))
        notebook.append_page(self._page_advanced(), Gtk.Label(label="Advanced"))

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_border_width(12)
        actions.set_halign(Gtk.Align.END)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.destroy())
        actions.pack_start(cancel, False, False, 0)

        save = Gtk.Button(label="Save")
        save.get_style_context().add_class("suggested-action")
        save.connect("clicked", self._on_save)
        actions.pack_start(save, False, False, 0)

        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        outer.pack_start(actions, False, False, 0)

    # -- tabs ---------------------------------------------------------------
    def _page_general(self) -> Gtk.Widget:
        cfg, page = self.cfg, _Page()

        self.lang_combo = Gtk.ComboBoxText()
        for code, label in LANGUAGES:
            self.lang_combo.append(code, f"{label}  ({code})")
        self.lang_combo.set_active_id(
            cfg["language"] if any(c == cfg["language"] for c, _ in LANGUAGES) else "auto"
        )
        page.add("Spoken language", self.lang_combo,
                 "Auto-detect copes with switching between languages; a fixed "
                 "language is slightly more reliable on very short clips.")

        self.translate_switch = Gtk.Switch(halign=Gtk.Align.START)
        self.translate_switch.set_active(bool(cfg["translate"]))
        page.add("Translate to English", self.translate_switch,
                 "Whisper's translate task only ever targets English. Off means "
                 "transcribe in whatever language was spoken.")

        self.output_combo = Gtk.ComboBoxText()
        for code, label in OUTPUT_MODES:
            self.output_combo.append(code, label)
        self.output_combo.set_active_id(cfg["output_mode"])
        page.add("Result", self.output_combo,
                 "Some Electron and Java apps ignore synthetic keystrokes; "
                 "keeping the clipboard copy gives you a fallback.")

        self.hotkey_entry = _entry(cfg.get("hotkey", "F8"), "F8, <Super>space, <Ctrl><Alt>d")
        page.add("Global shortcut", self.hotkey_entry,
                 "GTK accelerator syntax, applied on Save. Note F8/F9 are Step "
                 "Over and Resume in JetBrains IDEs.")

        self.mode_combo = Gtk.ComboBoxText()
        for code, label in (
            ("toggle", "Press to start, press again to stop"),
            ("hold", "Hold to talk, release to stop"),
        ):
            self.mode_combo.append(code, label)
        self.mode_combo.set_active_id(str(cfg.get("hotkey_mode", "toggle")))
        hint = ("Toggle suits long dictation; hold suits short phrases and "
                "cannot be left recording by accident.")
        if not _hold_supported():
            hint += "  (Hold needs python3-xlib, which is not installed - it "
            hint += "will fall back to toggle.)"
        page.add("Shortcut behaviour", self.mode_combo, hint)

        self.prompt_entry = _entry(cfg["initial_prompt"], "Angular, Firestore, Nx, ROYAL-321")
        page.add("Vocabulary hint", self.prompt_entry,
                 "Bias text for names and jargon that keep coming out wrong.")

        self.notify_combo = Gtk.ComboBoxText()
        for code, label in (
            ("none", "None - the panel icon says it all"),
            ("errors", "Errors only"),
            ("all", "Every state change"),
        ):
            self.notify_combo.append(code, label)
        self.notify_combo.set_active_id(str(cfg["notifications"]))
        page.add("Notifications", self.notify_combo,
                 "The red panel icon already shows when it is listening, so "
                 "per-recording toasts are mostly noise.")

        self.sound_check = Gtk.CheckButton(
            label="Play a sound when recording starts and stops")
        self.sound_check.set_tooltip_text(
            "Plays the sound files directly, so it works even with the "
            "desktop's global event sounds turned off.")
        self.sound_check.set_active(bool(cfg["play_sounds"]))
        page.add_wide(self.sound_check)

        self.lead_spin = _spin(cfg.get("sound_lead_in_ms", 600), 0, 2000, 50)
        page.add("Sound lead-in (ms)", self.lead_spin,
                 "Silence before each cue. Bluetooth headphones that have gone "
                 "idle swallow the first fraction of a second while the link "
                 "wakes up; raise this if the start cue sounds clipped. On "
                 "wired output 0 is fine and feels snappier.")

        self.focus_check = Gtk.CheckButton(label="Return focus to the original window before typing")
        self.focus_check.set_active(bool(cfg["restore_focus"]))
        page.add_wide(self.focus_check)
        return page

    def _page_audio(self) -> Gtk.Widget:
        cfg, page = self.cfg, _Page()

        from .core import input_devices

        self.device_combo = Gtk.ComboBoxText()
        self.device_combo.append("", "System default")
        known = {""}
        for name, desc in input_devices():
            self.device_combo.append(name, desc)
            known.add(name)
        current = str(cfg["input_device"])
        if current and current not in known:
            # A device that is not present right now (an unplugged headset, or a
            # Bluetooth mic that only exists in hands-free mode) must still be
            # selectable, or saving would silently reset it.
            self.device_combo.append(current, f"{current}  (not connected)")
        self.device_combo.set_active_id(current)
        page.add("Microphone", self.device_combo,
                 "A Bluetooth headset only appears here while it is in "
                 "hands-free mode; in A2DP it has no microphone at all.")

        self.headset_check = Gtk.CheckButton(
            label="Switch a Bluetooth headset to its microphone while recording")
        self.headset_check.set_active(bool(cfg["request_headset_mic"]))
        page.add_wide(self.headset_check)
        hint = Gtk.Label(
            label="Marks the recording as a call so the system switches the "
                  "headset to hands-free mode, then switches back. Costs about "
                  "a second per dictation, and while recording your headphone "
                  "audio drops to narrowband mono. Your laptop's own microphone "
                  "is usually the better choice for accuracy.",
            xalign=0, wrap=True)
        hint.set_max_width_chars(56)
        hint.get_style_context().add_class("dim-label")
        page.add_wide(hint)

        adj = Gtk.Adjustment(value=float(cfg["silence_threshold_db"]),
                             lower=-70, upper=-20, step_increment=1, page_increment=5)
        self.silence_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj)
        self.silence_scale.set_digits(0)
        self.silence_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.silence_scale.set_hexpand(True)
        page.add("Speech peak cutoff (dB)", self.silence_scale,
                 "A clip is only transcribed if its loudest moment reaches this. "
                 "Fed silence, Whisper invents fluent sentences, so this guard "
                 "matters. Lower it if quiet speech is being dropped.")

        self.spread_spin = _spin(cfg.get("min_speech_spread_db", 8.0), 0, 40, 1)
        page.add("Minimum speech spread (dB)", self.spread_spin,
                 "Required gap between the loudest moment and the noise floor. "
                 "Rejects steady background hum, which has a high level but no "
                 "dynamics. 8 dB works for a typical room.")

        self.latency_spin = _spin(cfg.get("capture_latency_ms", 20), 1, 500, 5)
        page.add("Capture latency (ms)", self.latency_spin,
                 "How quickly the mic starts. PulseAudio's default buffering "
                 "costs ~2s and loses the first second of speech; 20ms makes it "
                 "near-instant. Raise only if audio drops out under heavy load.")

        self.maxrec_spin = _spin(cfg["max_recording_s"], 10, 3600, 30)
        page.add("Maximum recording (s)", self.maxrec_spin,
                 "Safety net so a forgotten session cannot record forever.")

        self.delay_spin = _spin(cfg["type_delay_ms"], 0, 60, 1)
        page.add("Typing delay (ms)", self.delay_spin,
                 "Milliseconds between simulated keystrokes. Increase if an app "
                 "drops characters.")
        return page

    def _page_engine(self) -> Gtk.Widget:
        cfg, page = self.cfg, _Page()

        self.manage_check = Gtk.CheckButton(
            label="Start the speech engine on demand and stop it when idle")
        self.manage_check.set_active(bool(cfg.get("manage_engine", True)))
        page.add_wide(self.manage_check)

        self.idle_spin = _spin(cfg.get("engine_idle_timeout_min", 15), 0, 240, 5)
        page.add("Release VRAM after (min)", self.idle_spin,
                 "The engine holds about 3.5 GB of VRAM. 0 keeps it loaded "
                 "permanently. The first dictation after a shutdown costs ~4s.")

        self.server_entry = _entry(cfg["server_url"])
        page.add("Server URL", self.server_entry,
                 "If unreachable, the slower command-line path is used instead.")

        self.timeout_spin = _spin(cfg["server_timeout_s"], 5, 600, 10)
        page.add("Server timeout (s)", self.timeout_spin)
        return page

    def _page_advanced(self) -> Gtk.Widget:
        cfg, page = self.cfg, _Page()

        self.model_entry = _entry(cfg["model"])
        page.add("Model file", self.model_entry,
                 "Use large-v3 for translation. large-v3-turbo is faster but was "
                 "trained for transcription only and translates noticeably worse.")

        self.cli_entry = _entry(cfg["whisper_cli"])
        page.add("whisper-cli path", self.cli_entry,
                 "Used only when the server is unavailable.")

        self.threads_spin = _spin(cfg["cli_threads"], 1, 32, 1)
        page.add("Fallback CPU threads", self.threads_spin,
                 "Caps CPU use of the fallback path so it does not disturb "
                 "other work. The GPU does the heavy lifting anyway.")

        path_label = Gtk.Label(label=f"Config file: {CONFIG_PATH}", xalign=0)
        path_label.get_style_context().add_class("dim-label")
        path_label.set_selectable(True)
        path_label.set_line_wrap(True)
        page.add_wide(path_label)
        return page

    # -- save ---------------------------------------------------------------
    def _on_save(self, *_args) -> None:
        c = self.cfg
        c["language"] = self.lang_combo.get_active_id() or "auto"
        c["translate"] = self.translate_switch.get_active()
        c["output_mode"] = self.output_combo.get_active_id() or "type_and_copy"
        c["hotkey"] = self.hotkey_entry.get_text().strip()
        c["hotkey_mode"] = self.mode_combo.get_active_id() or "toggle"
        c["initial_prompt"] = self.prompt_entry.get_text().strip()
        c["notifications"] = self.notify_combo.get_active_id() or "errors"
        c["play_sounds"] = self.sound_check.get_active()
        c["sound_lead_in_ms"] = int(self.lead_spin.get_value())
        c["restore_focus"] = self.focus_check.get_active()

        c["silence_threshold_db"] = round(self.silence_scale.get_value(), 1)
        c["min_speech_spread_db"] = float(self.spread_spin.get_value())
        c["capture_latency_ms"] = int(self.latency_spin.get_value())
        c["input_device"] = self.device_combo.get_active_id() or ""
        c["request_headset_mic"] = self.headset_check.get_active()
        c["max_recording_s"] = int(self.maxrec_spin.get_value())
        c["type_delay_ms"] = int(self.delay_spin.get_value())

        c["manage_engine"] = self.manage_check.get_active()
        c["engine_idle_timeout_min"] = int(self.idle_spin.get_value())
        c["server_url"] = self.server_entry.get_text().strip() or c["server_url"]
        c["server_timeout_s"] = int(self.timeout_spin.get_value())

        c["model"] = self.model_entry.get_text().strip() or c["model"]
        c["whisper_cli"] = self.cli_entry.get_text().strip() or c["whisper_cli"]
        c["cli_threads"] = int(self.threads_spin.get_value())

        c.save()
        if self.on_saved:
            self.on_saved()
        self.destroy()
