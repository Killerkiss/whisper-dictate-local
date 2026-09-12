"""Recording, transcription and text output.

Deliberately free of any GTK import so the same code backs both the tray app
and the headless CLI toggle, and free of any OS-specific command: which binary
records audio or types text is `backend`'s problem, not this module's.
"""

from __future__ import annotations

import array
import logging
import math
import subprocess
import time
import wave
from pathlib import Path

import requests

from . import backend
from .backend import STATE_DIR, DictationError  # re-exported: callers import them here
from .config import Config

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000  # whisper resamples anything else, so feed it what it wants
CHANNELS = 1
SAMPLE_WIDTH = 2  # s16le

# The engine needs roughly 4s to load large-v3, so wait rather than starting a
# competing CLI run that would load its own copy of the model.
ENGINE_WAIT_ATTEMPTS = 6
ENGINE_WAIT_INTERVAL_S = 1.5


class Recorder:
    """Wraps a `parecord` child process writing raw PCM."""

    def __init__(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.raw_path = STATE_DIR / "rec.raw"
        self.wav_path = STATE_DIR / "rec.wav"
        self._proc: subprocess.Popen | None = None
        self.source_window: str | None = None
        self._headset = backend.HeadsetSwitch()

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(
        self,
        remember_focus: bool = True,
        latency_ms: int = 20,
        device: str = "",
        headset_mic: bool = False,
    ) -> None:
        if self.active:
            return
        self.raw_path.unlink(missing_ok=True)
        self.wav_path.unlink(missing_ok=True)

        # Remember where the text should land. Opening a tray menu moves focus
        # to the panel, so without this the transcript can be typed into the
        # wrong window.
        self.source_window = backend.active_window() if remember_focus else None

        # Put a Bluetooth headset into hands-free mode and record from it. In
        # A2DP it has no microphone at all, so without this the clip silently
        # comes from the laptop's own mic instead.
        if headset_mic and not device:
            headset_source = self._headset.engage()
            if headset_source:
                device = headset_source

        cmd = backend.record_command(
            self.raw_path,
            sample_rate=SAMPLE_RATE,
            channels=CHANNELS,
            latency_ms=latency_ms,
            device=device,
            headset_mic=headset_mic,
        )

        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def wait_until_live(self, timeout_s: float = 1.0) -> None:
        """Block until audio actually starts landing.

        PulseAudio takes roughly a second to connect the stream; telling the
        user to speak before that clips the first word.
        """
        deadline = timeout_s / 0.005
        i = 0
        while i < deadline:
            if not self.active:
                return
            if self.raw_path.exists() and self.raw_path.stat().st_size > 0:
                return
            i += 1
            time.sleep(0.005)

    def stop(self) -> Path:
        """Stop recording and return a playable WAV path."""
        if self._proc is None:
            raise DictationError("not recording")

        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5)
        proc, self._proc = self._proc, None
        self._headset.restore()

        if not self.raw_path.exists() or self.raw_path.stat().st_size == 0:
            stderr = (proc.stderr.read().decode(errors="replace") if proc.stderr else "").strip()
            raise DictationError(f"no audio captured{': ' + stderr if stderr else ''}")

        _raw_to_wav(self.raw_path, self.wav_path)
        return self.wav_path

    def cancel(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        self._headset.restore()
        self.raw_path.unlink(missing_ok=True)
        self.wav_path.unlink(missing_ok=True)


# Device enumeration is PulseAudio-specific; the backend owns the parsing.
input_devices = backend.input_devices


def _raw_to_wav(raw_path: Path, wav_path: Path) -> None:
    """Add a WAV header.

    parecord can write WAV directly, but killing it mid-write leaves a
    truncated header that whisper rejects; recording raw avoids that entirely.
    """
    pcm = raw_path.read_bytes()
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)


def level_dbfs(wav_path: Path) -> float:
    """Mean RMS level of a WAV in dBFS. Silence returns -inf.

    Kept for reporting; `speech_levels` is what the silence gate uses, because a
    whole-clip mean is dominated by the pauses either side of a short phrase.
    """
    with wave.open(str(wav_path), "rb") as wav:
        frames = wav.readframes(wav.getnframes())
    if not frames:
        return float("-inf")

    samples = array.array("h")
    samples.frombytes(frames[: len(frames) - (len(frames) % 2)])
    if not samples:
        return float("-inf")

    total = 0.0
    for s in samples:
        total += float(s) * float(s)
    rms = math.sqrt(total / len(samples))
    if rms <= 0:
        return float("-inf")
    return 20.0 * math.log10(rms / 32768.0)


def speech_levels(wav_path: Path, window_s: float = 0.05) -> tuple[float, float]:
    """Return (peak_db, noise_floor_db) measured over short windows.

    Peak is the loudest window, floor the 10th percentile. Comparing the two is
    far more reliable than a whole-clip average: a short phrase surrounded by
    silence averages out to near-silence, but its peak still stands well clear
    of the floor.
    """
    with wave.open(str(wav_path), "rb") as wav:
        rate = wav.getframerate() or SAMPLE_RATE
        frames = wav.readframes(wav.getnframes())

    samples = array.array("h")
    samples.frombytes(frames[: len(frames) - (len(frames) % 2)])
    if not samples:
        return float("-inf"), float("-inf")

    win = max(1, int(rate * window_s))
    levels: list[float] = []
    for i in range(0, max(1, len(samples) - win), win):
        chunk = samples[i : i + win]
        if not chunk:
            continue
        rms = math.sqrt(sum(float(x) * x for x in chunk) / len(chunk))
        levels.append(20.0 * math.log10(rms / 32768.0) if rms > 0 else -120.0)

    if not levels:
        return float("-inf"), float("-inf")

    levels.sort()
    floor = levels[min(len(levels) - 1, int(len(levels) * 0.10))]
    return levels[-1], floor


def has_speech(wav_path: Path, cfg: Config) -> tuple[bool, float, float]:
    """Decide whether a clip contains speech.

    Whisper invents fluent sentences when handed silence, so this must reject
    an empty room. Requires both an absolute peak and a clear gap between peak
    and noise floor, which keeps it working across mic gains and rooms.
    """
    peak, floor = speech_levels(wav_path)
    threshold = float(cfg["silence_threshold_db"])
    min_spread = float(cfg.get("min_speech_spread_db", 8.0))
    ok = peak >= threshold and (peak - floor) >= min_spread
    return ok, peak, floor


def duration_s(wav_path: Path) -> float:
    with wave.open(str(wav_path), "rb") as wav:
        return wav.getnframes() / float(wav.getframerate() or SAMPLE_RATE)


def transcribe(wav_path: Path, cfg: Config) -> str:
    """Resident server first, CLI fallback if it is not answering.

    The engine is started alongside the tray and needs a few seconds to load the
    model, so a connection error is retried before giving up. Falling back too
    eagerly would load a second copy of the model into VRAM while the first one
    is still loading.
    """
    last_exc: Exception | None = None
    for attempt in range(ENGINE_WAIT_ATTEMPTS):
        try:
            return _transcribe_server(wav_path, cfg)
        except requests.ConnectionError as exc:
            last_exc = exc
            if attempt < ENGINE_WAIT_ATTEMPTS - 1:
                log.info("engine not up yet, retrying (%d)", attempt + 1)
                time.sleep(ENGINE_WAIT_INTERVAL_S)
        except requests.RequestException as exc:
            # A reachable server that errored is a real failure, not a warm-up.
            last_exc = exc
            break

    log.warning("whisper server unavailable (%s); falling back to CLI", last_exc)
    return _transcribe_cli(wav_path, cfg)


def _transcribe_server(wav_path: Path, cfg: Config) -> str:
    data = {
        "temperature": "0.0",
        "response_format": "text",
        "no_timestamps": "true",
    }
    # Always send the language. whisper's own default is "en", NOT auto-detect,
    # so omitting this for "auto" silently forces English output - which looks
    # correct while translating and silently wrong when transcribing.
    data["language"] = cfg["language"] or "auto"
    # Likewise send translate explicitly rather than relying on a default.
    data["translate"] = "true" if cfg["translate"] else "false"
    if cfg["initial_prompt"]:
        data["prompt"] = cfg["initial_prompt"]

    with wav_path.open("rb") as fh:
        resp = requests.post(
            cfg["server_url"],
            files={"file": (wav_path.name, fh, "audio/wav")},
            data=data,
            timeout=cfg["server_timeout_s"],
        )
    resp.raise_for_status()
    return clean_text(resp.text)


def _transcribe_cli(wav_path: Path, cfg: Config) -> str:
    binary = cfg.path("whisper_cli")
    model = cfg.path("model")
    if not binary.exists():
        raise DictationError(f"whisper-cli not found at {binary}")
    if not model.exists():
        raise DictationError(f"model not found at {model}")

    cmd = [
        str(binary),
        "-m", str(model),
        "-f", str(wav_path),
        "--no-timestamps",
        "--no-prints",
        "-t", str(cfg["cli_threads"]),
    ]
    # Same trap as the server: whisper-cli defaults to "en", so "auto" must be
    # passed through explicitly rather than omitted.
    cmd += ["-l", cfg["language"] or "auto"]
    if cfg["translate"]:
        cmd.append("--translate")
    if cfg["initial_prompt"]:
        cmd += ["--prompt", cfg["initial_prompt"]]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise DictationError(f"whisper-cli failed: {result.stderr.strip()[:200]}")
    return clean_text(result.stdout)


def clean_text(text: str) -> str:
    """Strip whisper's non-speech markers and collapse whitespace."""
    import re

    text = re.sub(r"\[[^\]]*\]", " ", text)  # [BLANK_AUDIO], [Music]
    text = re.sub(r"\([^)]*\)", " ", text)   # (speaking in Ukrainian)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def deliver(text: str, cfg: Config, window: str | None = None) -> None:
    """Put the transcript where the user asked for it.

    Typing is the only part that can be unavailable (Wayland forbids it), so
    when it is asked for and impossible the text is copied instead rather than
    silently lost -- the user still gets their words, one Ctrl+V away.
    """
    mode = cfg["output_mode"]
    wants_type = mode in ("type", "type_and_copy")
    can_type = backend.TOOLS.can_type

    # Copy when asked to, and also as the fallback when typing is impossible.
    if mode in ("copy", "type_and_copy") or (wants_type and not can_type):
        backend.copy_text(text)

    if wants_type:
        if not can_type:
            raise DictationError("cannot type on this session; copied to the clipboard")
        if cfg["restore_focus"] and window:
            backend.focus_window(window)
        backend.type_text(text, delay_ms=int(cfg["type_delay_ms"]))
