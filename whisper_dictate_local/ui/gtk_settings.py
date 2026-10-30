"""GTK settings window.

Every value in the config file is editable here; nothing requires hand-editing
JSON. Grouped into tabs because a single column got unreadable once the audio
and engine knobs were added.

The dialog offers only what this machine can actually do. Anything the backend
reports as unavailable is either dropped from its list of choices or greyed out
with the reason in place of its usual hint, so the settings can never promise
behaviour the system will not deliver -- a Wayland session, for instance, shows
no "type it" option at all rather than accepting one that silently fails.
"""

from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Gtk, Pango  # noqa: E402

from .. import backend, history  # noqa: E402
from ..config import CONFIG_PATH, Config  # noqa: E402
from ..history import DB_PATH as HISTORY_PATH  # noqa: E402


def _hold_supported() -> bool:
    """Hold-to-talk needs to see the key release, which means reading the X
    keymap directly. No Xlib, or no X session, and only toggle is possible."""
    if not backend.TOOLS.can_grab_hotkey:
        return False
    try:
        from . import gtk_keystate as keystate

        return keystate.available()
    except Exception:  # noqa: BLE001 - the dialog must open regardless
        return False


# Shared with the macOS front end so the two cannot offer different choices.
from .options import LANGUAGES, output_modes as _output_modes  # noqa: E402


class _Page(Gtk.Grid):
    """One tab: label / widget / hint rows."""

    def __init__(self) -> None:
        super().__init__(row_spacing=8, column_spacing=14)
        self.set_border_width(18)
        self._row = 0

    def add(self, label: str, widget: Gtk.Widget, hint: str = "",
            unavailable: str = "") -> Gtk.Widget:
        lbl = Gtk.Label(label=label, xalign=0)
        lbl.set_valign(Gtk.Align.START)
        self.attach(lbl, 0, self._row, 1, 1)
        self.attach(widget, 1, self._row, 1, 1)
        self._row += 1
        # The reason replaces the hint rather than joining it: advice on how to
        # tune a setting is noise when the setting cannot take effect at all.
        if unavailable:
            lbl.set_sensitive(False)
            widget.set_sensitive(False)
            hint = f"Not available here — {unavailable}."
        if hint:
            self._hint(hint, column=1)
        return widget

    def add_wide(self, widget: Gtk.Widget, hint: str = "",
                 unavailable: str = "") -> Gtk.Widget:
        self.attach(widget, 0, self._row, 2, 1)
        self._row += 1
        if unavailable:
            widget.set_sensitive(False)
            hint = f"Not available here — {unavailable}."
        if hint:
            self._hint(hint, column=0, width=2)
        return widget

    def _hint(self, text: str, column: int = 1, width: int = 1) -> None:
        h = Gtk.Label(label=text, xalign=0, wrap=True)
        h.set_max_width_chars(56 if width > 1 else 50)
        h.get_style_context().add_class("dim-label")
        self.attach(h, column, self._row, width, 1)
        self._row += 1


def _spin(value: float, lo: float, hi: float, step: float = 1, digits: int = 0) -> Gtk.SpinButton:
    adj = Gtk.Adjustment(value=float(value), lower=lo, upper=hi,
                         step_increment=step, page_increment=step * 5)
    sb = Gtk.SpinButton(adjustment=adj, climb_rate=1, digits=digits)
    sb.set_halign(Gtk.Align.START)
    return sb


def _track(combo: Gtk.ComboBoxText) -> Gtk.ComboBoxText:
    """Remember whether the user changed this combo themselves.

    Needed because a filtered combo may show a substitute for a stored value
    this machine cannot perform. Saving the substitute would quietly destroy a
    preference that is valid on the user's other session -- dictating on a
    Wayland login would erase the X11 choice of "type it". Connected after the
    initial set_active_id so seeding the value does not count as a change.
    """
    combo.touched = False
    combo.connect("changed", lambda *_a: setattr(combo, "touched", True))
    return combo


def _entry(text: str, placeholder: str = "") -> Gtk.Entry:
    e = Gtk.Entry()
    e.set_text(str(text or ""))
    e.set_hexpand(True)
    if placeholder:
        e.set_placeholder_text(placeholder)
    return e


# Tab names a caller can open the dialog on.
PAGES = ("general", "audio", "engine", "advanced", "history")


class SettingsDialog(Gtk.Window):
    def __init__(self, cfg: Config, on_saved=None, page: str = "general") -> None:
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
        notebook.append_page(self._page_history(), Gtk.Label(label="History"))
        # Entries keep arriving while the dialog is open, so the list is read
        # again whenever its tab is shown rather than once at construction.
        notebook.connect("switch-page", self._on_switch_page)
        self.notebook = notebook
        self._start_page = PAGES.index(page) if page in PAGES else 0

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

    def show_all(self) -> None:
        super().show_all()
        # A notebook ignores set_current_page for pages not yet shown.
        self.notebook.set_current_page(self._start_page)

    # -- tabs ---------------------------------------------------------------
    def _banner(self, page: _Page) -> None:
        """Say once, at the top, what this session cannot do.

        Without it a greyed-out row looks like a bug in the app rather than a
        property of the session -- and the two have very different fixes.
        """
        tools = backend.TOOLS
        limits = []
        if not tools.can_type:
            limits.append("typing into other windows")
        if not tools.can_copy:
            limits.append("the clipboard")
        if not tools.can_grab_hotkey:
            limits.append("grabbing the shortcut")
        if not limits:
            return

        if len(limits) == 1:
            what, verb = limits[0], "is"
        else:
            what = f"{', '.join(limits[:-1])} and {limits[-1]}"
            verb = "are"
        session = "Wayland" if tools.session == "wayland" else tools.session.title()

        # A plain framed label rather than a Gtk.InfoBar: themes style even an
        # INFO bar in their error colour (Mint-Y paints it red), which turns a
        # statement of fact about the session into what looks like a fault.
        label = Gtk.Label(
            label=f"On this {session} session {what} {verb} unavailable. "
                  f"The settings below are greyed out accordingly.",
            xalign=0, wrap=True)
        label.set_max_width_chars(58)
        label.set_margin_top(4)
        label.set_margin_bottom(4)
        label.set_margin_start(10)
        label.set_margin_end(10)
        frame = Gtk.Frame()
        frame.add(label)
        page.add_wide(frame)

    def _page_general(self) -> Gtk.Widget:
        cfg, page = self.cfg, _Page()
        self._banner(page)

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
        modes = _output_modes()
        for code, label in modes:
            self.output_combo.append(code, label)
        available = {code for code, _ in modes}
        # A stored mode this system cannot carry out must not stay selected, or
        # Save would write back a setting that silently does nothing.
        self.output_combo.set_active_id(
            cfg["output_mode"] if cfg["output_mode"] in available
            else (modes[0][0] if modes else None)
        )
        _track(self.output_combo)
        if not modes:
            page.add("Result", self.output_combo,
                     unavailable="no way to type or copy was found; install "
                                 "xdotool or xclip")
        elif not backend.TOOLS.can_type:
            page.add("Result", self.output_combo,
                     f"Typing is not offered: {backend.TOOLS.why_not_type()}. "
                     "The transcript goes to the clipboard; paste with Ctrl+V.")
        else:
            page.add("Result", self.output_combo,
                     "Some Electron and Java apps ignore synthetic keystrokes; "
                     "keeping the clipboard copy gives you a fallback.")

        self.hotkey_entry = _entry(cfg.get("hotkey", "F8"), "F8, <Super>space, <Ctrl><Alt>d")
        if backend.TOOLS.can_grab_hotkey:
            page.add("Global shortcut", self.hotkey_entry,
                     "GTK accelerator syntax, applied on Save. Note F8/F9 are Step "
                     "Over and Resume in JetBrains IDEs.")
        else:
            page.add("Global shortcut", self.hotkey_entry,
                     unavailable="this session keeps shortcuts to itself. Bind a "
                                 "key to the 'whisper-dictate-local' command in your "
                                 "desktop's own keyboard settings instead")

        self.mode_combo = Gtk.ComboBoxText()
        hold_ok = _hold_supported()
        self.mode_combo.append("toggle", "Press to start, press again to stop")
        if hold_ok:
            self.mode_combo.append("hold", "Hold to talk, release to stop")
        stored = str(cfg.get("hotkey_mode", "toggle"))
        self.mode_combo.set_active_id(stored if hold_ok or stored == "toggle" else "toggle")
        _track(self.mode_combo)
        if hold_ok:
            page.add("Shortcut behaviour", self.mode_combo,
                     "Toggle suits long dictation; hold suits short phrases and "
                     "cannot be left recording by accident.")
        else:
            reason = ("Wayland exposes no way to see a key release"
                      if not backend.TOOLS.can_grab_hotkey
                      else "hold-to-talk needs python3-xlib, which is not installed")
            page.add("Shortcut behaviour", self.mode_combo,
                     f"Only toggle is possible here: {reason}.")

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
                 "per-recording toasts are mostly noise.",
                 unavailable="" if backend.TOOLS.can_notify
                 else "notify-send is not installed")

        self.sound_check = Gtk.CheckButton(
            label="Play a sound when recording starts and stops")
        self.sound_check.set_tooltip_text(
            "Plays the sound files directly, so it works even with the "
            "desktop's global event sounds turned off.")
        self.sound_check.set_active(bool(cfg["play_sounds"]))
        no_player = "" if backend.TOOLS.can_play else "no audio player was found"
        page.add_wide(self.sound_check, unavailable=no_player)

        self.lead_spin = _spin(cfg.get("sound_lead_in_ms", 600), 0, 2000, 50)
        page.add("Sound lead-in (ms)", self.lead_spin,
                 "Silence before each cue. Bluetooth headphones that have gone "
                 "idle swallow the first fraction of a second while the link "
                 "wakes up; raise this if the start cue sounds clipped. On "
                 "wired output 0 is fine and feels snappier.",
                 unavailable=no_player)

        self.focus_check = Gtk.CheckButton(label="Return focus to the original window before typing")
        self.focus_check.set_active(bool(cfg["restore_focus"]))
        # Pointless without typing, and impossible without a window manager we
        # can address -- which is every Wayland session.
        if not backend.TOOLS.can_type:
            focus_why = "there is no typing to restore focus for"
        elif not backend.TOOLS.can_restore_focus:
            focus_why = "this session does not let an app raise another's window"
        else:
            focus_why = ""
        page.add_wide(self.focus_check, unavailable=focus_why)
        return page

    def _page_audio(self) -> Gtk.Widget:
        cfg, page = self.cfg, _Page()

        from ..core import input_devices

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
                 "hands-free mode; in A2DP it has no microphone at all.",
                 unavailable="" if backend.TOOLS.can_list_devices
                 else "pactl is not installed, so the system default is used")

        self.headset_check = Gtk.CheckButton(
            label="Switch a Bluetooth headset to its microphone while recording")
        self.headset_check.set_active(bool(cfg["request_headset_mic"]))
        page.add_wide(
            self.headset_check,
            hint="Switches the headset to hands-free mode for the recording "
                 "and back afterwards, which is the only way it offers a "
                 "microphone at all — in A2DP it has none. Costs a fraction of "
                 "a second, and while recording your headphone audio drops to "
                 "narrowband mono. Your laptop's own microphone is usually the "
                 "better choice for accuracy.",
            unavailable="" if backend.TOOLS.can_switch_headset_profile
            else "profile switching is a PulseAudio feature and this machine "
                 "records with another tool",
        )

        self.pause_check = Gtk.CheckButton(
            label="Pause music and video while recording")
        self.pause_check.set_active(bool(cfg["pause_media_while_recording"]))
        page.add_wide(
            self.pause_check,
            hint="Pauses any running media player over MPRIS and resumes it "
                 "when the recording stops. Only players that were actually "
                 "playing are touched, so something you had already paused "
                 "stays that way. Worth turning on: audio bleeding into the "
                 "microphone is the easiest way to make Whisper invent text.",
            unavailable="" if backend.TOOLS.can_pause_media
            else "needs gdbus, and MPRIS does not exist on macOS",
        )

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
                 "drops characters.",
                 unavailable="" if backend.TOOLS.can_type
                 else backend.TOOLS.why_not_type())
        return page

    def _page_engine(self) -> Gtk.Widget:
        cfg, page = self.cfg, _Page()

        self.manage_check = Gtk.CheckButton(
            label="Start the speech engine on demand and stop it when idle")
        self.manage_check.set_active(bool(cfg.get("manage_engine", True)))
        engine = backend.make_engine(cfg)
        page.add_wide(
            self.manage_check,
            hint="Started as a systemd user service." if engine.kind == "systemd"
            else "No systemd here, so the engine runs as a child process of "
                 "the tray and is tracked by pidfile.",
        )

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

    def _page_history(self) -> Gtk.Widget:
        """Every transcript, newest first.

        Not a settings page like the others: deleting or clearing takes effect
        at once, because there is nothing sensible for Cancel to restore. Only
        the switch that turns recording on or off waits for Save.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(18)

        self.history_check = Gtk.CheckButton(label="Keep a history of transcripts")
        self.history_check.set_active(bool(self.cfg.get("keep_history", True)))
        box.pack_start(self.history_check, False, False, 0)

        hint = Gtk.Label(
            label="Saved before the text is typed or copied, so a dictation "
                  "that landed in the wrong window can be copied from here. "
                  "Stays on this computer.",
            xalign=0, wrap=True)
        hint.set_max_width_chars(60)
        hint.get_style_context().add_class("dim-label")
        box.pack_start(hint, False, False, 0)

        self.history_search = Gtk.SearchEntry()
        self.history_search.set_placeholder_text("Search transcripts")
        self.history_search.connect("search-changed", lambda *_: self._load_history())
        box.pack_start(self.history_search, False, False, 0)

        # id, when, text
        self.history_store = Gtk.ListStore(int, str, str)
        view = Gtk.TreeView(model=self.history_store)
        view.set_headers_visible(True)
        view.connect("row-activated", lambda *_: self._copy_selected())
        self.history_view = view

        when = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn("When", when, text=1)
        view.append_column(col)

        text = Gtk.CellRendererText()
        text.set_property("wrap-mode", Pango.WrapMode.WORD_CHAR)
        text.set_property("wrap-width", 380)
        col = Gtk.TreeViewColumn("Text", text, text=2)
        col.set_expand(True)
        view.append_column(col)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.add(view)
        box.pack_start(scroll, True, True, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        copy = Gtk.Button(label="Copy")
        copy.set_tooltip_text("Copy the selected transcript. Double-clicking a row does the same.")
        copy.connect("clicked", lambda *_: self._copy_selected())
        buttons.pack_start(copy, False, False, 0)

        delete = Gtk.Button(label="Delete")
        delete.connect("clicked", lambda *_: self._delete_selected())
        buttons.pack_start(delete, False, False, 0)

        clear = Gtk.Button(label="Clear all...")
        clear.connect("clicked", lambda *_: self._clear_history())
        buttons.pack_end(clear, False, False, 0)
        box.pack_start(buttons, False, False, 0)

        self.history_status = Gtk.Label(xalign=0)
        self.history_status.get_style_context().add_class("dim-label")
        self.history_status.set_selectable(True)
        self.history_status.set_line_wrap(True)
        box.pack_start(self.history_status, False, False, 0)

        self._load_history()
        return box

    def _on_switch_page(self, _notebook, _page, index: int) -> None:
        if PAGES[index] == "history":
            self._load_history()

    def _load_history(self) -> None:
        query = self.history_search.get_text().strip()
        entries = history.recent(query=query)
        self.history_store.clear()
        for e in entries:
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.created_at))
            self.history_store.append([e.id, stamp, e.text])
        if entries:
            shown = f"{len(entries)} shown"
        elif query:
            shown = "Nothing matches"
        else:
            shown = "No transcripts yet"
        self.history_status.set_text(f"{shown}. Stored in {HISTORY_PATH}")

    def _selected_history(self) -> tuple[int, str] | None:
        model, it = self.history_view.get_selection().get_selected()
        if it is None:
            return None
        return model[it][0], model[it][2]

    def _copy_selected(self) -> None:
        picked = self._selected_history()
        if picked is None:
            return
        # The GTK clipboard rather than backend.copy_text: this runs inside the
        # tray's own GTK process, which can own the selection without xclip,
        # and store() hands it to the clipboard manager so it outlives us.
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(picked[1], -1)
        clipboard.store()
        self.history_status.set_text("Copied to the clipboard.")

    def _delete_selected(self) -> None:
        picked = self._selected_history()
        if picked is None:
            return
        history.delete(picked[0])
        self._load_history()

    def _clear_history(self) -> None:
        confirm = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Delete every saved transcript?")
        confirm.format_secondary_text("This cannot be undone.")
        answer = confirm.run()
        confirm.destroy()
        if answer == Gtk.ResponseType.OK:
            history.clear()
            self._load_history()

    # -- save ---------------------------------------------------------------
    def _on_save(self, *_args) -> None:
        c = self.cfg
        c["language"] = self.lang_combo.get_active_id() or "auto"
        c["translate"] = self.translate_switch.get_active()
        # Only written when the user actually chose it, so a stored mode this
        # session cannot perform survives until they say otherwise. It is not
        # honoured meanwhile: deliver() falls back and reports what it did.
        if self.output_combo.touched and self.output_combo.get_active_id():
            c["output_mode"] = self.output_combo.get_active_id()
        c["hotkey"] = self.hotkey_entry.get_text().strip()
        if self.mode_combo.touched and self.mode_combo.get_active_id():
            c["hotkey_mode"] = self.mode_combo.get_active_id()
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
        c["pause_media_while_recording"] = self.pause_check.get_active()
        c["max_recording_s"] = int(self.maxrec_spin.get_value())
        c["type_delay_ms"] = int(self.delay_spin.get_value())

        c["manage_engine"] = self.manage_check.get_active()
        c["engine_idle_timeout_min"] = int(self.idle_spin.get_value())
        c["server_url"] = self.server_entry.get_text().strip() or c["server_url"]
        c["server_timeout_s"] = int(self.timeout_spin.get_value())

        c["model"] = self.model_entry.get_text().strip() or c["model"]
        c["whisper_cli"] = self.cli_entry.get_text().strip() or c["whisper_cli"]
        c["cli_threads"] = int(self.threads_spin.get_value())

        c["keep_history"] = self.history_check.get_active()

        c.save()
        if self.on_saved:
            self.on_saved()
        self.destroy()
