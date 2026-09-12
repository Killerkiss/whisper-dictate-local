"""Generate and cache the dictation cue sounds.

The cues are synthesised rather than shipped as fixed files because the useful
amount of leading silence depends on the output device. A Bluetooth sink that
has gone idle needs a few hundred milliseconds to wake up and swallows whatever
plays during that window, and how long varies by headset - so the lead-in is a
setting, and the files are regenerated whenever it changes.
"""

from __future__ import annotations

import array
import json
import logging
import math
import os
import wave
from pathlib import Path

log = logging.getLogger(__name__)

RATE = 48_000
AMPLITUDE = 0.45

CACHE_DIR = (
    Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    / "whisper-dictate-local"
    / "sounds"
)
STAMP = CACHE_DIR / ".generated.json"

FILES = ("start.wav", "stop.wav", "error.wav")


def _tone(freq: float, duration_s: float, amp: float = AMPLITUDE) -> list[float]:
    """A sine with short fades, so it cannot click at the edges."""
    n = int(RATE * duration_s)
    fade = max(1, int(RATE * 0.008))
    out = []
    for i in range(n):
        env = 1.0
        if i < fade:
            env = i / fade
        elif i > n - fade:
            env = max(0.0, (n - i) / fade)
        out.append(amp * env * math.sin(2 * math.pi * freq * i / RATE))
    return out


def _silence(duration_s: float) -> list[float]:
    return [0.0] * int(RATE * duration_s)


def _write(path: Path, samples: list[float]) -> None:
    pcm = array.array("h", (int(max(-1.0, min(1.0, s)) * 32767) for s in samples))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())


def generate(lead_in_ms: int, out_dir: Path = CACHE_DIR) -> None:
    """Write the three cues with the requested lead-in."""
    out_dir.mkdir(parents=True, exist_ok=True)
    lead = _silence(max(0, int(lead_in_ms)) / 1000.0)

    # Rising fifth for start, falling for stop: distinguishable without thinking.
    _write(out_dir / "start.wav", lead + _tone(660, 0.09) + _silence(0.02) + _tone(990, 0.11))
    _write(out_dir / "stop.wav", lead + _tone(990, 0.09) + _silence(0.02) + _tone(660, 0.11))
    _write(out_dir / "error.wav", lead + _tone(320, 0.14) + _silence(0.05) + _tone(240, 0.18))

    STAMP.write_text(json.dumps({"lead_in_ms": int(lead_in_ms)}), encoding="utf-8")
    log.info("cue sounds generated with %dms lead-in", lead_in_ms)


def ensure(lead_in_ms: int) -> Path:
    """Regenerate the cues if the lead-in changed or files are missing."""
    try:
        current = json.loads(STAMP.read_text(encoding="utf-8")).get("lead_in_ms")
    except (OSError, ValueError):
        current = None

    missing = any(not (CACHE_DIR / f).exists() for f in FILES)
    if missing or current != int(lead_in_ms):
        try:
            generate(lead_in_ms)
        except OSError as exc:
            log.warning("could not generate cue sounds: %s", exc)
    return CACHE_DIR
