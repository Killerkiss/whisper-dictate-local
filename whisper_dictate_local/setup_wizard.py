"""First-run setup: work out what the machine has, then fetch what it needs.

Packaging can install the app but not the two big things it depends on -- a
whisper.cpp server built for the hardware, and a multi-gigabyte model. The
model alone rules out bundling: large-v3 is 2.9 GiB, past GitHub's 2 GiB
per-asset limit and far past what belongs in a distribution package. So the
"it just works" part has to be a run-time step, and this is it.

Everything here is advisory and reversible: it only ever writes into the
config file and a directory under XDG_DATA_HOME, and `--dry-run` shows what
it would do without touching anything.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import requests

from . import backend
from .config import Config

DATA_DIR = (
    Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    / "whisper-dictate-local"
)
ENGINE_DIR = DATA_DIR / "whisper.cpp"
MODEL_DIR = DATA_DIR / "models"

MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{name}.bin"
RELEASES_API = "https://api.github.com/repos/ggml-org/whisper.cpp/releases"

# (name, approximate download size in MiB, what it needs, one-line rationale)
MODELS = [
    ("tiny", 75, 0.3, "fastest, noticeably worse"),
    ("base", 142, 0.5, "usable for short phrases"),
    ("small", 466, 1.0, "the sweet spot without a GPU"),
    ("medium", 1500, 2.6, "close to large, much cheaper"),
    ("large-v3-q5_0", 1100, 2.0, "large, quantised to fit a small card"),
    ("large-v3", 3095, 4.0, "best quality, and what the defaults assume"),
]


# -- reporting ---------------------------------------------------------------

def _say(message: str = "") -> None:
    print(message)


def _step(message: str) -> None:
    print(f"\n\033[1m{message}\033[0m" if sys.stdout.isatty() else f"\n{message}")


def _ask(question: str, default: bool, assume_yes: bool) -> bool:
    if assume_yes:
        _say(f"{question} [assuming yes]")
        return True
    if not sys.stdin.isatty():
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{question} {suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        _say()
        return False
    return default if not answer else answer.startswith("y")


# -- hardware detection ------------------------------------------------------

def _run(cmd: list[str], timeout: int = 10) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError):
        return ""


def detect_accelerator() -> tuple[str, str, float]:
    """(kind, description, usable memory in GiB).

    Memory is what limits the model choice: VRAM on a discrete card, system
    RAM everywhere else. Apple Silicon is unified, so the RAM figure is right
    for it too.
    """
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return "metal", "Apple Silicon (Metal)", _system_ram_gib()

    # nvidia-smi is the only one of these that reports memory exactly.
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits"])
    if out.strip():
        first = out.strip().splitlines()[0]
        name, _, mem = first.partition(",")
        # nvidia-smi already carries the vendor in most product names.
        label = name.strip()
        if not label.upper().startswith("NVIDIA"):
            label = f"NVIDIA {label}"
        try:
            return "cuda", label, float(mem.strip()) / 1024
        except ValueError:
            return "cuda", label, 0.0

    # AMD exposes VRAM through sysfs without any tool installed.
    for vram in Path("/sys/class/drm").glob("card*/device/mem_info_vram_total"):
        try:
            gib = int(vram.read_text().strip()) / (1024 ** 3)
        except (OSError, ValueError):
            continue
        if gib >= 1:
            return "hip", f"AMD GPU ({gib:.0f} GB VRAM)", gib

    if shutil.which("vulkaninfo"):
        out = _run(["vulkaninfo", "--summary"], timeout=20)
        match = re.search(r"deviceName\s*=\s*(.+)", out)
        if match and "llvmpipe" not in match.group(1).lower():
            return "vulkan", f"{match.group(1).strip()} (Vulkan)", _system_ram_gib()

    return "cpu", f"CPU only ({os.cpu_count() or '?'} cores)", _system_ram_gib()


def _system_ram_gib() -> float:
    if sys.platform == "darwin":
        out = _run(["sysctl", "-n", "hw.memsize"])
        try:
            return int(out.strip()) / (1024 ** 3)
        except ValueError:
            return 0.0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / (1024 ** 2)
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def recommend_model(kind: str, memory_gib: float) -> str:
    """Largest model that comfortably fits.

    Discrete GPUs are judged on VRAM; everything else on system RAM, with a
    deliberately conservative step down -- large-v3 on a CPU technically runs,
    but it is slow enough to make dictation pointless, which is a worse
    outcome than slightly lower accuracy.
    """
    if kind in ("cuda", "hip"):
        if memory_gib >= 6:
            return "large-v3"
        if memory_gib >= 4:
            return "large-v3-q5_0"
        if memory_gib >= 2:
            return "small"
        return "base"
    if kind in ("metal", "vulkan"):
        if memory_gib >= 16:
            return "large-v3"
        if memory_gib >= 8:
            return "large-v3-q5_0"
        return "small"
    # CPU: quality matters less than not waiting.
    return "small" if memory_gib >= 8 else "base"


BUILD_FLAGS = {
    "cuda": "-DGGML_CUDA=1",
    "hip": "-DGGML_HIP=ON",
    "vulkan": "-DGGML_VULKAN=1",
    "metal": "",          # on by default
    "cpu": "",
}


# -- downloads ---------------------------------------------------------------

def _download(url: str, dest: Path, label: str) -> bool:
    """Stream to a temporary file, then move it into place.

    Never writes a partial file to the final name: an interrupted 3 GB model
    download that looked complete would fail much later, inside whisper, with
    an error that says nothing about the real cause.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            done = 0
            with tmp.open("wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
                    done += len(chunk)
                    if total and sys.stdout.isatty():
                        pct = done * 100 // total
                        print(f"\r  {label}: {pct:3d}%  "
                              f"({done >> 20} / {total >> 20} MiB)", end="", flush=True)
        if sys.stdout.isatty():
            print()
        tmp.replace(dest)
        return True
    except (requests.RequestException, OSError) as exc:
        _say(f"  download failed: {exc}")
        tmp.unlink(missing_ok=True)
        return False


def prebuilt_asset_url() -> str | None:
    """Newest upstream Linux build of whisper.cpp, if one is published.

    Upstream attaches these only to its rolling bNNNN build tags; the
    semantic v1.x releases carry no binaries at all, so the newest release is
    the wrong thing to ask for.
    """
    if sys.platform != "linux":
        return None
    arch = platform.machine()
    wanted = {"x86_64": "whisper-bin-ubuntu-x64.tar.gz",
              "aarch64": "whisper-bin-ubuntu-arm64.tar.gz"}.get(arch)
    if wanted is None:
        return None
    try:
        r = requests.get(RELEASES_API, timeout=30, params={"per_page": 20})
        r.raise_for_status()
        for release in r.json():
            for asset in release.get("assets", []):
                if asset.get("name") == wanted:
                    return asset.get("browser_download_url")
    except (requests.RequestException, ValueError) as exc:
        _say(f"  could not reach the GitHub API: {exc}")
    return None


def install_prebuilt_engine(url: str) -> Path | None:
    """Unpack the upstream tarball and return the whisper-server path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        archive = Path(tmpdir) / "whisper.tar.gz"
        if not _download(url, archive, "whisper.cpp"):
            return None
        ENGINE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(archive) as tar:
                # Refuse absolute paths and ..; a malicious or malformed
                # archive must not write outside the target directory.
                safe = [m for m in tar.getmembers()
                        if not m.name.startswith("/") and ".." not in Path(m.name).parts]
                tar.extractall(ENGINE_DIR, members=safe)
        except (tarfile.TarError, OSError) as exc:
            _say(f"  could not unpack: {exc}")
            return None

    found = next(ENGINE_DIR.rglob("whisper-server"), None)
    if found is None:
        _say("  the archive contained no whisper-server")
        return None
    found.chmod(0o755)
    # The server dlopens its ggml backends from alongside itself.
    for lib in found.parent.glob("lib*.so*"):
        lib.chmod(0o755)
    return found



# -- keyboard shortcut -------------------------------------------------------

# (list schema, list key, per-binding schema, path prefix, list holds full paths)
SHORTCUT_SCHEMAS = [
    ("org.cinnamon.desktop.keybindings", "custom-list",
     "org.cinnamon.desktop.keybindings.custom-keybinding",
     "/org/cinnamon/desktop/keybindings/custom-keybindings/", False),
    ("org.gnome.settings-daemon.plugins.media-keys", "custom-keybindings",
     "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding",
     "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/", True),
]


def _gsettings(*args: str) -> str:
    return _run(["gsettings", *args]).strip()


def _parse_list(raw: str) -> list[str]:
    """Turn a GVariant string array into a Python list."""
    if not raw or raw.startswith("@as"):
        return []
    try:
        import ast

        value = ast.literal_eval(raw)
        return [str(v) for v in value]
    except (ValueError, SyntaxError):
        return []


def _shortcut_backend():
    if not shutil.which("gsettings"):
        return None
    schemas = _run(["gsettings", "list-schemas"])
    for entry in SHORTCUT_SCHEMAS:
        if entry[0] in schemas:
            return entry
    return None


def bind_shortcut(command: str, key: str) -> tuple[bool, str]:
    """Bind `key` to `command` as a custom desktop shortcut.

    Reuses an existing entry that already points at this app and otherwise
    allocates the first free slot. The old installer wrote custom0
    unconditionally, which silently destroyed whatever shortcut the user
    already had there.
    """
    backend_ = _shortcut_backend()
    if backend_ is None:
        return False, "no supported shortcut settings found (Cinnamon or GNOME)"
    list_schema, list_key, item_schema, prefix, full_paths = backend_

    existing = _parse_list(_gsettings("get", list_schema, list_key))

    slot = None
    for entry in existing:
        path = entry if entry.startswith("/") else f"{prefix}{entry}/"
        current = _gsettings("get", f"{item_schema}:{path}", "command")
        if "whisper-dictate-local" in current:
            slot = (entry, path)
            break

    if slot is None:
        used = {e.rstrip("/").rsplit("/", 1)[-1] if e.startswith("/") else e
                for e in existing}
        for i in range(64):
            name = f"custom{i}"
            if name not in used:
                entry = f"{prefix}{name}/" if full_paths else name
                slot = (entry, f"{prefix}{name}/")
                existing = existing + [entry]
                break
        if slot is None:
            return False, "no free shortcut slot"

    entry, path = slot
    schema_path = f"{item_schema}:{path}"
    _gsettings("set", schema_path, "name", "Dictation (toggle)")
    _gsettings("set", schema_path, "command", command)
    # Cinnamon stores an array of accelerators; GNOME a single string.
    binding = f"['{key}']" if not full_paths else f"'{key}'"
    _gsettings("set", schema_path, "binding", binding)
    _gsettings("set", list_schema, list_key, repr(existing).replace('"', "'"))

    check = _gsettings("get", f"{item_schema}:{path}", "command")
    if "whisper-dictate-local" not in check:
        return False, "the setting did not take; bind it by hand"
    return True, f"{key} -> {command}"


# -- the wizard ---------------------------------------------------------------

def run(assume_yes: bool = False, dry_run: bool = False) -> int:
    cfg = Config.load()

    _step("1. What this machine can do")
    for line in backend.TOOLS.describe():
        _say(f"  {line}")
    missing = [j for j, ok in (("typing", backend.TOOLS.can_type),
                               ("clipboard", backend.TOOLS.can_copy)) if not ok]
    if missing:
        _say(f"  note: no {' or '.join(missing)}; run --check for how to fix it")

    _step("2. Hardware")
    kind, description, memory = detect_accelerator()
    _say(f"  {description}")
    _say(f"  {memory:.1f} GiB of {'VRAM' if kind in ('cuda', 'hip') else 'memory'}")

    _step("3. Model")
    recommended = recommend_model(kind, memory)
    for name, size_mib, _need, why in MODELS:
        mark = "->" if name == recommended else "  "
        _say(f"  {mark} {name:16} {size_mib:>5} MiB   {why}")
    _say(f"\n  recommended: {recommended}")

    model_path = MODEL_DIR / f"ggml-{recommended}.bin"
    configured = cfg.path("model")
    if configured.exists():
        _say(f"  already have a model at {configured}; keeping it")
        model_path = configured
    elif model_path.exists():
        _say(f"  already downloaded: {model_path}")
    elif dry_run:
        _say(f"  would download {MODEL_URL.format(name=recommended)}")
    elif _ask(f"  Download {recommended}?", True, assume_yes):
        if not _download(MODEL_URL.format(name=recommended), model_path, recommended):
            return 1
    else:
        _say("  skipped; set `model` in the config yourself")

    _step("4. Speech engine")
    server = cfg.path("whisper_server")
    existing = next(ENGINE_DIR.rglob("whisper-server"), None) if ENGINE_DIR.exists() else None
    if server.exists():
        _say(f"  already have one at {server}; keeping it")
    elif existing is not None:
        _say(f"  already downloaded: {existing}")
        server = existing
    elif dry_run:
        _say(f"  would fetch the prebuilt server, or build with {BUILD_FLAGS[kind] or 'no extra flags'}")
    else:
        url = prebuilt_asset_url()
        if url and kind == "cpu" and _ask("  Download the prebuilt CPU server?", True, assume_yes):
            got = install_prebuilt_engine(url)
            if got is None:
                return 1
            server = got
        elif url and _ask(f"  A prebuilt server is available, but it is CPU-only and you\n"
                          f"  have {description}. Download it anyway?", False, assume_yes):
            got = install_prebuilt_engine(url)
            if got is None:
                return 1
            server = got
        else:
            _say(_build_instructions(kind))

    _step("5. Keyboard shortcut")
    command = shutil.which("whisper-dictate-local") or "whisper-dictate-local"
    key = str(cfg["hotkey"])
    if dry_run:
        _say(f"  would bind {key} to {command}")
    elif _shortcut_backend() is None:
        _say("  no Cinnamon or GNOME shortcut settings here.")
        _say(f"  Bind {key} to `{command}` in your desktop's keyboard settings.")
        _say("  On X11 the tray also grabs the key itself, so this is a fallback.")
    elif _ask(f"  Bind {key} to start and stop dictation?", True, assume_yes):
        ok, message = bind_shortcut(command, key)
        _say(f"  {'bound: ' if ok else 'could not bind: '}{message}")

    _step("6. Configuration")
    changed = []
    # Compare resolved paths: the config stores "~/opt/..." while these are
    # absolute, so comparing raw strings would rewrite settings that are
    # already correct and quietly expand every tilde in the file.
    if model_path.exists() and cfg.path("model") != model_path:
        cfg["model"] = str(model_path); changed.append("model")
    if server.exists() and cfg.path("whisper_server") != server:
        cfg["whisper_server"] = str(server); changed.append("whisper_server")
    if changed and not dry_run:
        cfg.save()
        _say(f"  updated {', '.join(changed)}")
    elif changed:
        _say(f"  would update {', '.join(changed)}")
    else:
        _say("  nothing to change")

    _step("Done")
    if model_path.exists() and server.exists():
        _say("  Everything is in place. Start the tray with:")
        _say("      whisper-dictate-local-tray &")
    else:
        _say("  Still missing pieces; see above. `--check` reports the rest.")
    return 0


def _build_instructions(kind: str) -> str:
    flags = BUILD_FLAGS[kind]
    return (
        "  No prebuilt server for this hardware, so build one:\n\n"
        "      git clone --depth 1 https://github.com/ggml-org/whisper.cpp \\\n"
        "          ~/opt/whisper.cpp\n"
        "      cd ~/opt/whisper.cpp\n"
        f"      cmake -B build -DCMAKE_BUILD_TYPE=Release{' ' + flags if flags else ''}\n"
        "      cmake --build build -j\n\n"
        "  then set `whisper_server` in the config to\n"
        "  ~/opt/whisper.cpp/build/bin/whisper-server"
    )
