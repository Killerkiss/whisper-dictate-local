"""Persistent configuration for whisper-dictate-local.

Stored as JSON in ~/.config/whisper-dictate-local/config.json so both the tray app and the
CLI entry point read the same settings.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
CONFIG_DIR = _CONFIG_HOME / "whisper-dictate-local"
CONFIG_PATH = CONFIG_DIR / "config.json"

# The project was called uk-dictate until it grew past a single input language.
# Settings are worth carrying over rather than silently resetting, so the old
# directory is moved across once, the first time the renamed app runs.
LEGACY_CONFIG_DIR = _CONFIG_HOME / "uk-dictate"


def migrate_legacy_config() -> bool:
    """Move a pre-rename config into place. True if anything was moved."""
    if CONFIG_DIR.exists() or not LEGACY_CONFIG_DIR.is_dir():
        return False
    try:
        CONFIG_DIR.parent.mkdir(parents=True, exist_ok=True)
        # shutil.move rather than rename: XDG_CONFIG_HOME and the home
        # directory are not guaranteed to be on the same filesystem.
        shutil.move(str(LEGACY_CONFIG_DIR), str(CONFIG_DIR))
    except OSError as exc:
        # Never fatal: the app still works, just with default settings.
        log.warning("could not migrate settings from %s (%s)", LEGACY_CONFIG_DIR, exc)
        return False
    log.info("settings migrated from %s to %s", LEGACY_CONFIG_DIR, CONFIG_DIR)
    return True

DEFAULTS: dict[str, Any] = {
    # Global shortcut, grabbed by the tray itself via Keybinder. Cinnamon's own
    # custom-shortcut manager proved unreliable at picking up changes written
    # outside its GUI, so the app owns the grab instead.
    "hotkey": "F8",
    # "toggle" - press to start, press again to stop
    # "hold"   - record only while the key is held down
    # Hold needs python3-xlib to see the key release, since the shortcut library
    # only ever reports presses; without it the app stays in toggle mode.
    "hotkey_mode": "toggle",
    # Input language whisper is told to expect. "auto" lets it detect, which is
    # less reliable on short clips but copes better with mixed-language speech.
    "language": "uk",
    # True  -> whisper's translate task, which always targets English.
    # False -> plain transcription in the spoken language.
    "translate": True,
    "server_url": "http://127.0.0.1:8910/inference",
    "server_timeout_s": 120,
    # Only used where systemd is unavailable, in which case the app starts the
    # server itself instead of through `systemctl --user`.
    "whisper_server": "~/opt/whisper.cpp/build/bin/whisper-server",
    # Fallback used only when the resident server is unreachable.
    "whisper_cli": "~/opt/whisper.cpp/build/bin/whisper-cli",
    "model": "~/opt/whisper.cpp/models/ggml-large-v3.bin",
    "cli_threads": 4,
    # type | copy | type_and_copy
    "output_mode": "type_and_copy",
    "type_delay_ms": 8,
    # Whisper invents fluent sentences when handed silence, so a clip must clear
    # both of these to be transcribed: a loudest-window peak above
    # silence_threshold_db, and at least min_speech_spread_db between that peak
    # and the noise floor. Peak+spread survives changes in mic gain and room
    # noise that a whole-clip average does not.
    "silence_threshold_db": -42.0,
    "min_speech_spread_db": 8.0,
    # PulseAudio's default capture buffer is huge: parecord takes ~2s to deliver
    # its first sample and silently drops the first second of a clip. Asking for
    # a small latency cuts that to ~50ms and captures the full window.
    "capture_latency_ms": 20,
    # PulseAudio/PipeWire source name, or "" for the system default.
    "input_device": "",
    # Tag the capture stream as a Communication stream. WirePlumber's bluetooth
    # policy switches a connected headset from A2DP to HSP/HFP for exactly these
    # streams, and switches back when the stream closes - which is how you get
    # the headset mic at all, since A2DP exposes no microphone.
    # Off by default: HSP/HFP is narrowband mono, and switching also degrades
    # whatever is playing for as long as the recording lasts.
    "request_headset_mic": False,
    "max_recording_s": 300,
    # Start the speech engine on first use rather than at login, and shut it
    # down again once idle. The model costs ~3.5 GB of VRAM, worth reclaiming on
    # a laptop. The price is ~4s on the first dictation after an idle gap.
    "manage_engine": True,
    "engine_idle_timeout_min": 15,
    "restore_focus": True,
    "play_sounds": True,
    # Silence prepended to each cue. A Bluetooth sink that has gone idle needs
    # a few hundred ms to wake and swallows anything playing during that time,
    # so the tone has to start after the wake-up. Raise it if the start cue
    # sounds clipped; lower it for snappier feedback on wired output.
    "sound_lead_in_ms": 600,
    # "all"    - every state change (noisy: a toast per recording)
    # "errors" - only failures worth acting on
    # "none"   - silent; the panel icon is the only feedback
    "notifications": "errors",
    # Optional bias text; useful for names or jargon whisper keeps mangling.
    "initial_prompt": "",
}


class Config:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data = dict(DEFAULTS)
        if data:
            self._data.update({k: v for k, v in data.items() if k in DEFAULTS})
        # notifications used to be a bool; keep old config files working.
        if isinstance(self._data.get("notifications"), bool):
            self._data["notifications"] = "all" if self._data["notifications"] else "none"

    # -- dict-ish access ----------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)

    # -- resolved paths -----------------------------------------------------
    def path(self, key: str) -> Path:
        return Path(os.path.expanduser(str(self._data[key])))

    # -- persistence --------------------------------------------------------
    @classmethod
    def load(cls) -> "Config":
        migrate_legacy_config()
        if not CONFIG_PATH.exists():
            cfg = cls()
            cfg.save()
            return cfg
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # A corrupt config must not stop dictation from working.
            log.warning("Could not read %s (%s); falling back to defaults", CONFIG_PATH, exc)
            return cls()
        return cls(raw)

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(CONFIG_PATH)  # atomic, so a crash mid-write cannot truncate it
