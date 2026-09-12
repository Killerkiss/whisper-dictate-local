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
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

STATE_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "whisper-dictate-local"


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
        # macOS has no notify-send; osascript raises a Notification Centre
        # banner, which is the closest equivalent and needs nothing installed.
        self.notifier = _which("osascript") if is_macos() else _which("notify-send")

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
        """Needs pactl to change the card profile and parecord to capture from
        it. A headset in A2DP has no microphone at all, so this is what makes
        one usable rather than a mere quality setting."""
        return (self.device_lister is not None
                and self.recorder is not None
                and Path(self.recorder).name == "parecord")

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
    communication_role: bool = False,
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
        if communication_role:
            # Asks the desktop to move a Bluetooth headset to HSP/HFP for the
            # duration of this stream. Only set when the app could NOT switch
            # the profile itself: WirePlumber applies the switch on a delay,
            # which lands after our own restore and leaves the headset stuck
            # in hands-free mode. See HeadsetSwitch.
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
    if is_macos():
        return _avfoundation_devices()
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


def _avfoundation_devices() -> list[tuple[str, str]]:
    """Audio capture devices as macOS numbers them.

    ffmpeg addresses avfoundation inputs by index, not name, so the index is
    what gets stored in the config -- hence a device can change identity if
    one is unplugged. Listing is a deliberate error: the command has no
    "just list" mode and always exits non-zero after printing to stderr.
    """
    if TOOLS.recorder is None or Path(TOOLS.recorder).name != "ffmpeg":
        return []
    try:
        result = subprocess.run(
            [TOOLS.recorder, "-hide_banner", "-f", "avfoundation",
             "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("could not list avfoundation devices: %s", exc)
        return []

    devices: list[tuple[str, str]] = []
    in_audio = False
    for line in result.stderr.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if "AVFoundation video devices" in line:
            in_audio = False
            continue
        if not in_audio:
            continue
        match = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line)
        if match:
            devices.append((f":{match.group(1)}", match.group(2)))
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


# -- bluetooth headset profile ------------------------------------------------

# A2DP is output-only: a headset in it exposes no microphone at all. HSP/HFP
# does, at narrowband mono quality. Preference order is best codec first.
HEADSET_PROFILES = ("headset-head-unit-msbc", "headset-head-unit-cvsd",
                    "headset-head-unit")


def _pactl(*args: str) -> str:
    if TOOLS.device_lister is None:
        return ""
    try:
        env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
        r = subprocess.run([TOOLS.device_lister, *args], capture_output=True,
                           text=True, timeout=15, env=env)
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("pactl %s failed: %s", " ".join(args), exc)
        return ""


def bluetooth_card() -> str | None:
    """Name of a connected Bluetooth audio card, if there is one."""
    for line in _pactl("list", "cards", "short").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith("bluez_card."):
            return parts[1]
    return None


def _card_state(card: str) -> tuple[str, set[str]]:
    """(active profile, profiles the card offers right now)."""
    active, available = "", set()
    in_card = False
    for line in _pactl("list", "cards").splitlines():
        stripped = line.strip()
        if stripped.startswith("Name: "):
            in_card = stripped.split(": ", 1)[1] == card
        if not in_card:
            continue
        if stripped.startswith("Active Profile: "):
            active = stripped.split(": ", 1)[1]
        # Profile lines look like "headset-head-unit-msbc: Headset (...)".
        match = re.match(r"^([a-z0-9+_-]+): .*available: yes", stripped)
        if match:
            available.add(match.group(1))
    return active, available


class HeadsetSwitch:
    """Put a Bluetooth headset into hands-free mode around a recording.

    WirePlumber is supposed to do this itself when a stream declares
    media.role=Communication, and the recorder still sets that property. But
    the policy is off by default on some builds -- Ubuntu 24.04's WirePlumber
    0.4.17 ships an empty bluez rule set -- and even switched on it did not
    engage on the hardware this was tested against. Doing it explicitly is a
    few lines and works the same everywhere.
    """

    def __init__(self) -> None:
        self.card: str | None = None
        self.previous: str | None = None

    def engage(self) -> str | None:
        """Switch to HSP/HFP. Returns the source to record from, or None.

        None means "carry on with the normal input" -- no headset, already in
        hands-free, or the switch failed. It is never an error: a missing
        headset should cost the user a worse microphone, not a lost recording.
        """
        card = bluetooth_card()
        if card is None:
            return None

        active, available = _card_state(card)
        if active.startswith("headset-head-unit"):
            return self._await_source()  # already there; nothing to restore

        target = next((p for p in HEADSET_PROFILES if p in available), None)
        if target is None:
            log.info("%s offers no hands-free profile", card)
            return None

        if not _pactl_set_profile(card, target):
            return None
        self.card, self.previous = card, active
        log.info("bluetooth: %s -> %s", active, target)
        return self._await_source()

    def _await_source(self, timeout_s: float = 5.0) -> str | None:
        """Wait for the headset's capture source to become usable.

        Existence is not enough. For the first second or so after the profile
        change the node is listed with a state of "(null)" -- present, but not
        yet set up -- and recording from it then yields a silent, empty file
        with no error from parecord at all. Waiting for a real state is the
        difference between this working and failing silently.
        """
        deadline = time.monotonic() + timeout_s
        seen = None
        while time.monotonic() < deadline:
            for line in _pactl("list", "sources", "short").splitlines():
                parts = line.split()
                if len(parts) < 3 or not parts[1].startswith("bluez_input."):
                    continue
                seen = parts[1]
                if parts[-1].upper() in ("IDLE", "SUSPENDED", "RUNNING"):
                    return parts[1]
            time.sleep(0.1)
        if seen:
            log.warning("bluetooth source %s never became ready", seen)
        else:
            log.warning("no bluetooth capture source appeared")
        return None

    def restore(self) -> None:
        """Put the previous profile back, so music sounds right again."""
        if self.card is None or self.previous is None:
            return
        target = self.previous
        if target == "off" or target.startswith("headset-head-unit"):
            # A card caught mid-connection reports "off". Restoring that would
            # switch the headset off entirely -- worse than where we started --
            # so fall back to the best output profile it offers.
            _active, available = _card_state(self.card)
            target = _best_output_profile(available) or ""
            if not target:
                log.info("bluetooth: nothing sensible to restore to; leaving as is")
                self.card = self.previous = None
                return
        # Verify rather than assume. A restore that silently fails leaves the
        # headset in narrowband mono for the rest of the session, which is the
        # worst outcome here -- worse than never having switched at all.
        card = self.card
        self.card = self.previous = None
        for attempt in (1, 2):
            _pactl_set_profile(card, target)
            active, _ = _card_state(card)
            if active == target:
                log.info("bluetooth: restored %s", target)
                return
            if attempt == 1:
                time.sleep(0.5)
        log.warning("bluetooth: could not restore %s (still %s); "
                    "set it back in your sound settings", target, active)


# Best first: SBC-XQ beats plain SBC, and both beat anything non-A2DP.
OUTPUT_PROFILES = ("a2dp-sink-sbc_xq", "a2dp-sink-aptx_hd", "a2dp-sink-aptx",
                   "a2dp-sink-aac", "a2dp-sink-sbc", "a2dp-sink")


def _best_output_profile(available: set[str]) -> str | None:
    for profile in OUTPUT_PROFILES:
        if profile in available:
            return profile
    return next((p for p in sorted(available) if p.startswith("a2dp")), None)


def _pactl_set_profile(card: str, profile: str) -> bool:
    if TOOLS.device_lister is None:
        return False
    try:
        r = subprocess.run([TOOLS.device_lister, "set-card-profile", card, profile],
                           capture_output=True, text=True, timeout=15)
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("could not set %s to %s: %s", card, profile, exc)
        return False
    if r.returncode != 0:
        log.warning("could not set %s to %s: %s", card, profile, r.stderr.strip())
        return False
    return True


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
    if Path(TOOLS.notifier).name == "osascript":
        # Text is passed as arguments rather than interpolated into the script:
        # a transcript containing a quote would otherwise be a syntax error at
        # best, and arbitrary AppleScript at worst.
        script = ('on run argv\n'
                  'display notification (item 1 of argv) with title (item 2 of argv)\n'
                  'end run')
        subprocess.run([TOOLS.notifier, "-e", script, message, title], check=False)
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

    def unit_installed(self) -> bool:
        """Whether the unit file exists at all.

        A package install puts the app on the system without writing a user
        unit -- only install.sh does that -- so systemd being present is not
        enough to conclude it manages the engine.
        """
        return self._run("cat", self.SERVICE).returncode == 0

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
    """Pick the engine manager this system can actually use.

    systemd is preferred only when the unit is actually installed. Without
    that check a packaged install -- which ships no unit -- would hand every
    start to systemctl, watch it fail, and quietly fall back to the CLI path
    that reloads the whole model on every phrase.
    """
    if TOOLS.systemd is not None:
        systemd = SystemdEngine(TOOLS.systemd)
        if systemd.unit_installed():
            return systemd
        log.info("no whisper-server user unit; managing the engine directly")
    else:
        log.info("systemctl unavailable; managing the speech engine directly")
    return ProcessEngine(cfg.path("whisper_server"), cfg.path("model"),
                         str(cfg["server_url"]))
