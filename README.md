# uk-dictate

Push-a-key dictation for Linux: speak Ukrainian, get English text typed into
whatever window you are using. Everything runs locally on the GPU via
[whisper.cpp](https://github.com/ggml-org/whisper.cpp) — no API keys, no audio
leaving the machine.

Developed on Linux Mint / Cinnamon, and works on any **X11** desktop.
Wayland is limited to clipboard output — see [Compatibility](#compatibility).

## How it works

```
global shortcut ──► uk-dictate ──► tray app (SIGUSR1)
                                      │
                        parecord ─────┤ raw PCM
                                      │
                          silence gate │  (RMS dBFS)
                                      │
                    whisper-server ───┤  HTTP, model stays resident
                                      │
                         xdotool type ┘  + clipboard copy
```

Two shortcut behaviours, switchable in Settings:

- **Toggle** (default) - press to start, press again to stop. Good for longer
  dictation; you are not holding a key down while you think.
- **Hold to talk** - records only while the key is held. Good for short phrases,
  and it cannot be left recording by accident.

Hold mode needs `python3-xlib`. Shortcut libraries report key *presses* only, so
the release is detected by reading the X keymap directly (`XQueryKeymap`) every
40ms. Without Xlib the app stays in toggle mode and says so.

## Compatibility

The thing that decides whether this works is **your session type, not your
distribution**. The app types text with `xdotool`, grabs the hotkey with
Keybinder and reads held keys with `XQueryKeymap` — all three are X11. That line
runs straight through Ubuntu and Fedora rather than around them, because both
ship a Wayland session by default.

| Session | Result |
|---|---|
| **X11 / Xorg** | everything works: typing, hold-to-talk, focus restore |
| **Wayland** | clipboard output only — see below |

### Verified

- **Linux Mint 22 (Cinnamon, X11)** — the development machine. Everything below
  is tested here.

### Expected to work unchanged

Any X11 session on a systemd distribution with a panel that shows app
indicators. Only the package names differ:

| Distribution | Notes |
|---|---|
| Mint (Cinnamon / MATE / Xfce) | X11 by default — nothing to change |
| Debian 12, Ubuntu **on Xorg** | pick "Ubuntu on Xorg" at the login screen |
| Kubuntu / KDE Plasma **on X11** | Plasma shows indicators natively |
| Fedora Xfce / Cinnamon spins | X11 by default |
| Arch, EndeavourOS, Manjaro (X11) | — |
| openSUSE (X11) | — |
| Pop!_OS | X11 by default |

These follow from the code rather than from testing: the installer uses only
freedesktop standard paths (`~/.local/bin`, `~/.config/autostart`, hicolor
icons, `.desktop` files), and audio playback already falls back across
`paplay` → `pw-play` → `aplay`, so PulseAudio and PipeWire are both fine.
If you run one of these, a report either way is welcome.

**GNOME needs an extension.** GNOME Shell removed the legacy tray, so the panel
icon requires `gnome-shell-extension-appindicator`. Ubuntu ships and enables it
by default; on stock Fedora GNOME you must install it yourself.

### Wayland — partially

Not a packaging gap; Wayland deliberately forbids one client from injecting
input into another. On a Wayland session:

- **Typing does not work.** Set *Result* to **Copy** in Settings and paste with
  Ctrl+V. `wl-copy` is needed instead of `xclip`.
- **Hold-to-talk does not work** — there is no equivalent of `XQueryKeymap`.
  Use toggle mode.
- **Focus restore does not work** and is unnecessary in copy mode.
- **The global shortcut must come from your compositor.** Bind your key to the
  `uk-dictate` command in the desktop's own keyboard settings; the CLI signals
  the running tray, so behaviour is identical to the built-in grab.

Full typing support on Wayland needs `ydotool` (which needs `/dev/uinput`
access) or `wtype` (wlroots compositors only). Neither is wired up yet.

### Not supported

- **macOS / BSD.** `parecord`, `xdotool` and the GTK app indicator have no
  equivalent there. macOS also needs a different audio backend and a menu bar
  UI rather than a GTK tray.
- **Distributions without systemd** (Void, Devuan, Alpine, Gentoo/OpenRC).
  The app will start, but it cannot manage the speech engine as a user service
  and silently falls back to the CLI path, which reloads the 3.5 GB model on
  every dictation. Start `whisper-server` yourself and it behaves normally.

### Dependencies by distribution

Runtime packages, once whisper.cpp is built. Names are a starting point and may
drift between releases.

```bash
# Debian / Ubuntu / Mint
sudo apt install python3-gi python3-requests python3-xlib \
    gir1.2-ayatanaappindicator3-0.1 gir1.2-keybinder-3.0 \
    pulseaudio-utils xdotool xclip

# Fedora
sudo dnf install python3-gobject python3-requests python3-xlib \
    libayatana-appindicator-gtk3 keybinder3 \
    pulseaudio-utils xdotool xclip

# Arch
sudo pacman -S python-gobject python-requests python-xlib \
    libayatana-appindicator libkeybinder3 \
    libpulse xdotool xclip

# openSUSE
sudo zypper install python3-gobject python3-requests python3-xlib \
    typelib-1_0-AyatanaAppIndicator3-0_1 typelib-1_0-Keybinder-3_0 \
    pulseaudio-utils xdotool xclip
```

`python3-xlib` is only needed for hold-to-talk; without it the app stays in
toggle mode and says so. On Wayland, swap `xclip` for `wl-clipboard`.

## Requirements

Everything except whisper.cpp is present on a stock Mint 22 install; see
[Dependencies by distribution](#dependencies-by-distribution) for other systems.

- GTK 3 with `python3-gi`, an app indicator library, and Keybinder
- `python3-xlib` (only for hold-to-talk)
- `pulseaudio-utils` (`parecord`), `xdotool`, `xclip`
- whisper.cpp built with CUDA, plus a GGML model

### Building whisper.cpp

```bash
sudo apt install cmake nvidia-cuda-toolkit build-essential
git clone --depth 1 https://github.com/ggml-org/whisper.cpp ~/opt/whisper.cpp
cd ~/opt/whisper.cpp
# CUDA 12.0 (Ubuntu 24.04) will not accept gcc-13, hence the host compiler pin.
# Set CMAKE_CUDA_ARCHITECTURES to your GPU: 89 = RTX 40xx, 86 = RTX 30xx.
cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=1 \
      -DCMAKE_CUDA_ARCHITECTURES=89 \
      -DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++-12
cmake --build build -j"$(nproc)"
curl -L -o models/ggml-large-v3.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin
```

Use **large-v3**, not large-v3-turbo. Turbo is faster but was trained for
transcription only, and its translation quality is noticeably worse.

## Install

```bash
./install.sh          # DICTATE_KEY=F8 ./install.sh to pick another shortcut
uk-dictate-tray &
```

This symlinks the launchers into `~/.local/bin`, enables the speech engine as a
user service, autostarts the tray, and binds the shortcut.

## Panel icon

| state | icon |
|---|---|
| idle | microphone, themed to match the panel |
| recording | **red** microphone |
| transcribing | amber microphone |

## Settings

Right-click the panel icon → **Settings**. Every option lives in the UI across
four tabs — General, Audio, Engine, Advanced — and nothing needs the JSON to be
edited by hand. Stored in `~/.config/uk-dictate/config.json`.

| Setting | Notes |
|---|---|
| Spoken language | `uk` by default. `auto` handles mixed speech but is less reliable on short clips. |
| Translate to English | On: English out, whatever you speak. Off: the text comes out in the language you spoke. Whisper's translate task only ever targets English - other target languages would need a separate translation step. |
| Result | Type it, copy it, or both. Some Electron/Java apps ignore synthetic keystrokes — the clipboard copy is the fallback. |
| Global shortcut | GTK accelerator syntax (`F8`, `<Super>space`). The app grabs it itself via Keybinder rather than relying on the desktop's shortcut manager. |
| Shortcut behaviour | Toggle, or hold-to-talk. |
| Silence cutoff | A clip must clear this peak level *and* show at least 8 dB between peak and noise floor. **Do not disable**: fed silence, Whisper invents fluent sentences. |
| Typing delay | Raise if an app drops characters. |
| Vocabulary hint | Bias text for names and jargon Whisper mangles. |
| Notifications | `none` / `errors` / `all`. Defaults to `errors`: the red panel icon already shows when it is listening, so a toast per recording is just noise. |

## Design notes

- **Raw PCM, not WAV, from `parecord`.** Killing `parecord` mid-write leaves a
  truncated WAV header that whisper rejects; the header is added afterwards.
- **Capture latency is set explicitly.** PulseAudio's default capture buffer is
  enormous: plain `parecord` takes ~2s to deliver its first sample and silently
  drops the first second of a clip. Measured over a 3s window: 2.00s captured by
  default, 3.00s with `--latency-msec=20`, and time-to-first-sample falls from
  ~2000ms to ~50ms. The tray still waits for the stream to go live before
  prompting, but that wait is now negligible.
- **The engine is started on demand and shut down when idle.** Loading large-v3
  costs ~3.4s and ~3.5 GB of VRAM; keeping it resident cuts a dictation to
  ~0.5s. The tray starts it on your first dictation and stops it after the idle
  timeout, so a laptop GPU is not holding the model all day. Measured: 1223 MiB
  idle, 5077 MiB with the engine loaded.
- **A cold request waits for the engine rather than falling back immediately.**
  Otherwise the CLI fallback would load a second copy of the model into VRAM
  while the service was still loading its own.
- **Focus is captured at record time** and restored before typing, so using the
  tray menu does not send text to the panel.
- **Silence gate uses peak + spread, not a whole-clip average.** A short
  phrase surrounded by pauses averages out to near-silence; measured on a real
  6s Ukrainian phrase, the mean was -47 dB (below a -45 gate) while the peak was
  -38.6 dB against a -51 dB floor. Peak-vs-floor also survives changes in mic
  gain, which an absolute average does not.
- **Language is always sent explicitly.** whisper's own default is `en`, not
  auto-detect, so omitting the parameter for "auto" silently forces English
  output. That looks correct while translating and is silently wrong when
  transcribing, which made it easy to miss.
- **Cue sounds start with 350ms of silence.** A Bluetooth sink that has gone
  idle takes a few hundred milliseconds to wake and swallows whatever plays
  during that window - which silently ate the entire 0.31s start cue while the
  1.09s stop cue survived. The bundled cues (`sounds/`, regenerate with
  `tools/make_sounds.py`) pad the front with silence and use a rising interval
  for start and a falling one for stop, so the two are distinguishable without
  thinking.
- **Sound cues bypass libcanberra.** canberra honours the desktop's global
  "event sounds" switch and refuses with *Sound disabled* when it is off, which
  silently disables this app's cues too. The files are played directly instead,
  with distinct sounds for start and stop.
- **The app owns the global hotkey.** Cinnamon's custom-shortcut manager did not
  reliably pick up bindings written via `gsettings` outside its GUI, so the tray
  grabs the key with Keybinder instead.

## Getting good results

- **Speak a full sentence.** Whisper uses context; it hallucinates far more on
  one- or two-word fragments than on connected speech.
- **You can speak immediately.** Capture is live ~50ms after the keypress.
  On a loud mechanical keyboard a very short pause still helps, but only to keep
  the key click out of the clip - the click alone is loud enough to pass the
  speech gate, and a clip of nothing but clicks makes Whisper invent text.
- **Check the mic level before blaming the model.** Measured on this machine:

  | mic volume | ambient noise | verdict |
  |---|---|---|
  | 18% | -63 dB | speech never reaches the gate |
  | 40% | -52 dB | usable |
  | 65% | -45 dB | good |
  | 100% | -28 dB | noise transients pass as speech |

  `pactl set-source-volume @DEFAULT_SOURCE@ 65%`

### Bluetooth headsets

A Bluetooth headset in A2DP (the high quality music profile) has **no
microphone at all** - the profile is output-only. The mic only exists under
HSP/HFP, which is a different profile the system has to switch into.

Enable *Switch a Bluetooth headset to its microphone while recording* and the
capture stream is tagged `media.role=Communication`. WirePlumber's bluetooth
policy switches the headset to HSP/HFP for exactly those streams and restores
the previous profile when the stream closes, so no manual switching is needed.

Two conditions and one trade-off:

- The headset must be the **current audio output**; the policy only acts when
  the default sink is the Bluetooth device.
- HSP/HFP is narrowband mono (CVSD 8 kHz, or mSBC 16 kHz where supported).
  While recording, whatever you are listening to drops to that quality, and the
  switch itself costs about a second each way.
- A laptop's built-in digital microphone is full-band and usually gives Whisper
  **better accuracy** than a Bluetooth headset mic. Prefer it unless the headset
  is clearly better positioned for your voice.

### Spotting a hallucination

If the output looks plausible but unrelated to what you said - especially
"Thanks for watching!", "Дякую за перегляд!", "Subtitles by ..." - Whisper got
no intelligible audio and guessed. These phrases are artefacts of YouTube
subtitles in its training data. Check the mic, not the settings.

## Troubleshooting

```bash
journalctl --user -u whisper-server -f     # engine logs
uk-dictate --no-tray                       # bypass the tray
systemctl --user restart whisper-server
```

Nothing typed? Check the clipboard — the app that had focus probably ignores
synthetic key events.

VRAM needed elsewhere? The engine releases it automatically after the idle
timeout (Settings -> *Release VRAM after*), or stop it now from the tray menu.
Dictation still works with it stopped: the first phrase pays ~4s to load the
model, then it is fast again.
