"""Platform and desktop-session backends.

Every call that shells out to an OS-specific tool lives here, so the rest of
the app can stay written in terms of *what* it wants ("type this text") rather
than *which binary* does it on this machine.

Selection is by capability, never by distribution name: an Arch box and a Mint
box with the same tools installed take the same path, and the same X11 code
runs on FreeBSD. What genuinely differs is the display server -- Wayland
forbids one client injecting input into another, so typing and key-state
polling have no equivalent there and the app degrades to clipboard output.

Detection runs once at import. Installing a tool while the app is running is
rare enough that paying `shutil.which` on every keystroke is not worth it;
call `refresh()` if a settings dialog needs to re-check.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

STATE_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "uk-dictate"


class DictationError(Exception):
    """Anything the user should be told about in plain language."""


# -- session detection -------------------------------------------------------

def session_type() -> str:
    """"x11", "wayland" or "unknown".

    XDG_SESSION_TYPE is set by every modern login manager, but a session
    started from a bare `startx` leaves it empty, so fall back to which display
    variable is populated. WAYLAND_DISPLAY is checked first because an Xwayland
    session sets DISPLAY as well.
    """
    declared = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if declared in ("x11", "wayland"):
        return declared
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def is_macos() -> bool:
    return sys.platform == "darwin"


def _which(*names: str) -> str | None:
    """First of these binaries that is installed."""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


class Tools:
    """Which implementation of each job is available on this machine."""

    def __init__(self) -> None:
        self.session = session_type()

        # Text injection. On Wayland this is a protocol restriction, not a
        # missing package: ydotool works by writing to /dev/uinput underneath
        # the compositor, and wtype needs a wlroots compositor (Sway, Hyprland)
        # -- neither works on GNOME or KDE Wayland.
        if is_macos():
            self.typer = _which("osascript")
        elif self.session == "wayland":
            self.typer = _which("wtype", "ydotool")
        else:
            self.typer = _which("xdotool")

        if is_macos():
            self.clipboard = _which("pbcopy")
        elif self.session == "wayland":
            self.clipboard = _which("wl-copy", "xclip", "xsel")
        else:
            self.clipboard = _which("xclip", "xsel", "wl-copy")

        self.recorder = _which("parecord", "ffmpeg")
        self.player = _which("paplay", "pw-play", "aplay", "afplay")
        self.device_lister = _which("pactl")
        # Window focus is X11-only. Wayland exposes no way to identify or
        # re-activate another client's window.
        self.window = _which("xdotool") if self.session == "x11" else None
        self.systemd = _which("systemctl")
        self.notifier = _which("notify-send")

    # -- capabilities ------------------------------------------------------
    # The settings UI asks these rather than testing for tools itself, so what
    # the dialog offers and what the app can do cannot drift apart.

    @property
    def can_type(self) -> bool:
        return self.typer is not None

    @property
    def can_copy(self) -> bool:
        return self.clipboard is not None

    @property
    def can_restore_focus(self) -> bool:
        return self.window is not None

    @property
    def can_grab_hotkey(self) -> bool:
        """Keybinder grabs keys through X11. A Wayland compositor keeps its
        shortcuts to itself, so the key has to be bound in its own settings."""
        return self.session == "x11"

    @property
    def can_list_devices(self) -> bool:
        return self.device_lister is not None

    @property
    def can_play(self) -> bool:
        return self.player is not None

    @property
    def can_notify(self) -> bool:
        return self.notifier is not None

    @property
    def can_switch_headset_profile(self) -> bool:
        """Tagging a stream as a call is a PulseAudio/WirePlumber feature; it is
        what makes a Bluetooth headset expose a microphone at all."""
        return self.recorder is not None and Path(self.recorder).name == "parecord"

    def why_not_type(self) -> str:
        """One phrase explaining the lack of typing, for the settings UI."""
        if self.can_type:
            return ""
        if self.session == "wayland":
            return "Wayland does not let one app type into another; install wtype or ydotool"
        if is_macos():
            return "osascript is unavailable"
        return "xdotool is not installed"

    def describe(self) -> list[str]:
        """Human-readable capability report, for logs and Troubleshooting."""
        lines = [f"session: {self.session}"]
        for job, tool in (
            ("type", self.typer),
            ("clipboard", self.clipboard),
            ("record", self.recorder),
            ("play", self.player),
            ("focus", self.window),
        ):
            lines.append(f"{job}: {Path(tool).name if tool else 'unavailable'}")
        lines.append(f"engine: {'systemd' if self.systemd else 'child process'}")
        return lines


TOOLS = Tools()


def refresh() -> Tools:
    """Re-run detection, e.g. after the user installs a missing package."""
    global TOOLS
    TOOLS = Tools()
    return TOOLS


# -- audio capture -----------------------------------------------------------

def record_command(
    raw_path: Path,
    sample_rate: int,
    channels: int,
    latency_ms: int = 20,
    device: str = "",
    headset_mic: bool = False,
) -> list[str]:
    """Command that writes raw s16le PCM to `raw_path` until killed."""
    tool = TOOLS.recorder
    if tool is None:
        raise DictationError("no audio recorder found (install pulseaudio-utils)")

    name = Path(tool).name
    if name == "parecord":
        cmd = [
            tool,
            # Without an explicit latency PulseAudio buffers so aggressively
            # that the first ~1s of every clip is lost.
            f"--latency-msec={max(1, int(latency_ms))}",
            "--format=s16le",
            f"--rate={sample_rate}",
            f"--channels={channels}",
        ]
        if headset_mic:
            # WirePlumber switches a Bluetooth headset to HSP/HFP only for
            # streams marked as Communication, and restores the previous
            # profile once the stream closes.
            cmd.append("--property=media.role=Communication")
        if device:
            cmd += ["-d", device]
        return cmd + ["--raw", str(raw_path)]

    # ffmpeg fallback, for machines with no PulseAudio tooling and for macOS.
    input_spec = ["-f", "avfoundation", "-i", device or ":0"] if is_macos() else \
                 ["-f", "pulse", "-i", device or "default"]
    return [
        tool, "-hide_banner", "-loglevel", "error", "-y",
        *input_spec,
        "-ac", str(channels), "-ar", str(sample_rate),
        "-f", "s16le", str(raw_path),
    ]


def input_devices() -> list[tuple[str, str]]:
    """Available capture devices as (name, description), monitors excluded."""
    if TOOLS.device_lister is None:
        return []
    try:
        # pactl localises its field labels ("Name:" becomes "Назва:" under a
        # Ukrainian locale), so force a neutral locale before parsing.
        env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
        result = subprocess.run(
            [TOOLS.device_lister, "list", "sources"],
            capture_output=True, text=True, timeout=10, env=env,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("could not list sources: %s", exc)
        return []

    devices: list[tuple[str, str]] = []
    name = desc = ""
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("Name:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("Description:"):
            desc = line.split(":", 1)[1].strip()
            if name and not name.endswith(".monitor"):
                devices.append((name, desc or name))
            name = desc = ""
    return devices


def play(path: Path) -> None:
    """Fire off a cue sound without waiting for it."""
    if TOOLS.player is None:
        log.warning("no audio player available to play cues")
        return
    try:
        subprocess.Popen([TOOLS.player, str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log.warning("could not play %s: %s", path, exc)


# -- text output -------------------------------------------------------------

def active_window() -> str | None:
    """Identifier of the focused window, or None where that is unknowable."""
    if TOOLS.window is None:
        return None
    try:
        out = subprocess.run([TOOLS.window, "getactivewindow"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except (subprocess.SubprocessError, OSError):
        return None


def focus_window(window: str) -> None:
    if TOOLS.window is None:
        return
    subprocess.run([TOOLS.window, "windowactivate", "--sync", window],
                   capture_output=True, timeout=5, check=False)


def type_text(text: str, delay_ms: int = 8) -> None:
    """Type `text` into whatever has focus."""
    tool = TOOLS.typer
    if tool is None:
        raise DictationError(
            "typing is not available on this session; use clipboard output"
            if TOOLS.session == "wayland" else "'xdotool' is not installed"
        )

    name = Path(tool).name
    if name == "xdotool":
        cmd = [tool, "type", "--clearmodifiers", "--delay", str(delay_ms), "--", text]
    elif name == "wtype":
        cmd = [tool, "-d", str(delay_ms), "--", text]
    elif name == "ydotool":
        cmd = [tool, "type", "--key-delay", str(delay_ms), "--", text]
    elif name == "osascript":
        cmd = [tool, "-e",
               'on run argv\ntell application "System Events" to keystroke (item 1 of argv)\nend run',
               text]
    else:  # pragma: no cover - Tools only ever selects the names above
        raise DictationError(f"don't know how to type with {name}")

    subprocess.run(cmd, capture_output=True, timeout=120, check=False)


def copy_text(text: str) -> None:
    tool = TOOLS.clipboard
    if tool is None:
        log.warning("no clipboard tool installed (xclip, wl-clipboard)")
        return

    name = Path(tool).name
    args = {
        "xclip": [tool, "-selection", "clipboard"],
        "xsel": [tool, "--clipboard", "--input"],
        "wl-copy": [tool],
        "pbcopy": [tool],
    }.get(name, [tool])

    # stdout and stderr MUST be closed, not piped. On X11 and Wayland the
    # clipboard has no storage of its own: the process that ran the copy stays
    # alive to serve the selection on request. xclip forks such a child, and
    # that child inherits any pipe we hand it -- so capture_output=True waits
    # for EOF that only arrives when the clipboard contents are replaced,
    # blocking the whole dictation until the timeout fires.
    try:
        subprocess.run(args, input=text.encode(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10, check=False)
    except subprocess.TimeoutExpired:
        # Never fatal: the transcript is worth more than the clipboard.
        log.warning("%s did not return; clipboard may not be set", name)


def notify(title: str, message: str, icon: str = "audio-input-microphone") -> None:
    if TOOLS.notifier is None:
        return
    subprocess.run([TOOLS.notifier, "-t", "2500", "-i", icon, title, message],
                   check=False)


# -- speech engine service ---------------------------------------------------

class Engine:
    """Starts and stops the resident whisper.cpp server.

    Two implementations because not every system has systemd. The fallback
    matters more than it looks: without it the app silently drops to the CLI
    path, which reloads a 3.5 GB model from disk on *every single dictation*.
    """

    kind = "none"

    def is_running(self) -> bool:
        raise NotImplementedError

    def start(self) -> tuple[bool, str]:
        """(started, message). False means it was already up or failed."""
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


class SystemdEngine(Engine):
    kind = "systemd"
    SERVICE = "whisper-server.service"

    def __init__(self, systemctl: str) -> None:
        self._systemctl = systemctl

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([self._systemctl, "--user", *args],
                              capture_output=True, text=True, check=False)

    def is_running(self) -> bool:
        return self._run("is-active", "--quiet", self.SERVICE).returncode == 0

    def start(self) -> tuple[bool, str]:
        result = self._run("start", self.SERVICE)
        if result.returncode == 0:
            return True, "speech engine started on demand"
        return False, result.stderr.strip()

    def stop(self) -> None:
        self._run("stop", self.SERVICE)


class ProcessEngine(Engine):
    """Runs whisper-server as a plain child process, tracked by a pidfile.

    Used wherever `systemctl --user` is unavailable: Void, Devuan, Alpine,
    Gentoo/OpenRC, and inside containers.
    """

    kind = "process"

    def __init__(self, binary: Path, model: Path, url: str) -> None:
        self.binary = binary
        self.model = model
        parsed = urlparse(url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 8910
        self.pidfile = STATE_DIR / "engine.pid"

    def _pid(self) -> int | None:
        try:
            pid = int(self.pidfile.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        try:
            os.kill(pid, 0)
        except OSError:
            self.pidfile.unlink(missing_ok=True)  # stale
            return None
        return pid

    def is_running(self) -> bool:
        return self._pid() is not None

    def start(self) -> tuple[bool, str]:
        if self.is_running():
            return False, "already running"
        if not self.binary.exists():
            return False, f"whisper-server not found at {self.binary}"
        if not self.model.exists():
            return False, f"model not found at {self.model}"

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        log_path = STATE_DIR / "engine.log"
        try:
            with log_path.open("ab") as logfile:
                proc = subprocess.Popen(
                    [str(self.binary), "--model", str(self.model),
                     "--host", self.host, "--port", str(self.port),
                     "--threads", "4", "--convert"],
                    stdout=logfile, stderr=logfile, stdin=subprocess.DEVNULL,
                    # Own process group, so the engine survives this app exiting
                    # unexpectedly rather than dying mid-transcription.
                    start_new_session=True,
                )
        except OSError as exc:
            return False, str(exc)

        self.pidfile.write_text(str(proc.pid), encoding="utf-8")
        return True, f"speech engine started (pid {proc.pid}, log {log_path})"

    def stop(self) -> None:
        import signal as _signal

        pid = self._pid()
        if pid is None:
            return
        try:
            os.kill(pid, _signal.SIGTERM)
        except OSError as exc:
            log.warning("could not stop engine pid %d: %s", pid, exc)
        self.pidfile.unlink(missing_ok=True)


def make_engine(cfg) -> Engine:
    """Pick the engine manager this system can actually use."""
    if TOOLS.systemd is not None:
        return SystemdEngine(TOOLS.systemd)
    log.info("systemctl unavailable; managing the speech engine directly")
    return ProcessEngine(cfg.path("whisper_server"), cfg.path("model"),
                         str(cfg["server_url"]))
