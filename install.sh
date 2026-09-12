#!/usr/bin/env bash
# Install whisper-dictate-local for the current user. Safe to re-run.
#
# Branches on what the system is, never on what it is called: the desktop
# integration differs between a freedesktop desktop and macOS, and the speech
# engine is a systemd user service only where systemd exists. Everything else
# is shared.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"

WHISPER_BIN="${WHISPER_BIN:-$HOME/opt/whisper.cpp/build/bin/whisper-server}"
MODEL="${MODEL:-$HOME/opt/whisper.cpp/models/ggml-large-v3.bin}"

say() { printf '  %s\n' "$*"; }

case "$(uname -s)" in
    Darwin)                      PLATFORM=macos ;;
    *BSD | DragonFly)            PLATFORM=bsd ;;
    *)                           PLATFORM=linux ;;
esac

echo "Installing whisper-dictate-local from $ROOT  ($PLATFORM)"

mkdir -p "$BIN_DIR"

# -- clean up the pre-rename install -----------------------------------------
# The project was called uk-dictate. Its autostart entry points at a launcher
# that no longer exists, so leaving it behind means a failed app at every login
# and a stale menu entry that looks like the real thing. Settings are NOT
# touched here: the app migrates ~/.config/uk-dictate itself on first run.
if [ "$PLATFORM" != "macos" ]; then
    LEGACY_FILES=(
        "$HOME/.config/autostart/uk-dictate-tray.desktop"
        # Earlier versions of this installer autostarted the tray. They no
        # longer do, so an entry left from then would start an app the user
        # never asked to have running.
        "$HOME/.config/autostart/whisper-dictate-local-tray.desktop"
        "$HOME/.local/share/applications/uk-dictate-tray.desktop"
        "$HOME/.local/share/icons/hicolor/scalable/apps/uk-dictate-recording.svg"
        "$HOME/.local/share/icons/hicolor/scalable/apps/uk-dictate-busy.svg"
        "$HOME/.local/share/icons/hicolor/scalable/apps/uk-dictate-idle-symbolic.svg"
        "$BIN_DIR/uk-dictate-tray"
    )
    removed=0
    for f in "${LEGACY_FILES[@]}"; do
        if [ -e "$f" ] || [ -L "$f" ]; then rm -f "$f"; removed=$((removed + 1)); fi
    done
    # Regenerated on demand from the configured lead-in, so not worth moving.
    rm -rf "$HOME/.local/share/uk-dictate"
    [ "$removed" -gt 0 ] && say "removed $removed leftover file(s) from a previous install"
fi

# -- launchers ---------------------------------------------------------------
ln -sf "$ROOT/bin/whisper-dictate-local"      "$BIN_DIR/whisper-dictate-local"
ln -sf "$ROOT/bin/whisper-dictate-local-tray" "$BIN_DIR/whisper-dictate-local-tray"
if [ "$PLATFORM" != "macos" ]; then
    # Kept so a keyboard shortcut still bound to the old `uk-dictate` command
    # keeps working. Delete it once the shortcut points at the new name.
    ln -sf "$ROOT/bin/whisper-dictate-local" "$BIN_DIR/uk-dictate"
fi
say "launchers -> $BIN_DIR"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) say "NOTE: $BIN_DIR is not on your PATH; add it to your shell profile" ;;
esac

# -- speech engine -----------------------------------------------------------
# With systemd it is a user service. Without one - macOS, BSD, musl distros -
# the app starts whisper-server itself and tracks it by pidfile, so there is
# nothing to install here.
if ! command -v systemctl >/dev/null; then
    say "no systemd; the app will start the speech engine itself"
    if [ ! -x "$WHISPER_BIN" ]; then
        say "NOTE: set whisper_server in the config - $WHISPER_BIN is not executable"
    fi
elif [ -x "$WHISPER_BIN" ] && [ -f "$MODEL" ]; then
    UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"
    sed -e "s|@WHISPER_BIN@|$WHISPER_BIN|g" -e "s|@MODEL@|$MODEL|g" \
        "$ROOT/systemd/whisper-server.service.in" > "$UNIT_DIR/whisper-server.service"
    systemctl --user daemon-reload
    # Deliberately NOT enabled: the tray starts the engine when it launches and
    # stops it on quit, so the model does not hold VRAM when you are not using it.
    systemctl --user disable whisper-server.service >/dev/null 2>&1 || true
    say "speech engine installed (started on demand by the tray)"
else
    say "WARNING: whisper-server or model not found; skipping the service."
    say "         expected binary: $WHISPER_BIN"
    say "         expected model:  $MODEL"
    say "         dictation will still work via the slower CLI fallback."
fi

# -- desktop integration -----------------------------------------------------
# Nothing here starts the app at login. A dictation tool holds a microphone
# and a global key grab; starting it unasked is the sort of thing a user
# should opt into, in their own desktop's startup settings, rather than have
# an installer decide for them. Add it there if you want it.
if [ "$PLATFORM" = "macos" ]; then
    # A LaunchAgent template ships in launchd/ for anyone who does want it;
    # see the README.
    say "not starting at login; add it to Login Items if you want that"
else
    ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
    APPS_DIR="$HOME/.local/share/applications"
    mkdir -p "$ICON_DIR" "$APPS_DIR"

    ICONS="$ROOT/whisper_dictate_local/icons"
    # The launcher icon, distinct from the three panel-state icons below: it
    # sits among other application icons, so it is full colour on a tile.
    cp "$ICONS/whisper-dictate-local.svg"           "$ICON_DIR/"
    cp "$ICONS/whisper-dictate-local-recording.svg" "$ICON_DIR/"
    cp "$ICONS/whisper-dictate-local-busy.svg"      "$ICON_DIR/"
    cp "$ICONS/whisper-dictate-local-idle.svg"      "$ICON_DIR/whisper-dictate-local-idle-symbolic.svg"
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
    say "icons installed"

    sed "s|@BIN_DIR@|$BIN_DIR|g" "$ROOT/desktop/whisper-dictate-local-tray.desktop.in" \
        > "$APPS_DIR/whisper-dictate-local-tray.desktop"
    chmod +x "$APPS_DIR/whisper-dictate-local-tray.desktop"
    say "menu entry -> $APPS_DIR (search the menu for \"Whisper Dictate\")"

    command -v update-desktop-database >/dev/null \
        && update-desktop-database "$APPS_DIR" 2>/dev/null || true

fi

echo
if [ "$PLATFORM" = "macos" ]; then
    cat <<'EOF'
Done. Two things macOS needs from you:

  1. Microphone access. The first dictation will ask; allow it.
  2. Accessibility, for typing the text and for the global shortcut:
     System Settings -> Privacy & Security -> Accessibility, then add the
     terminal (or the Python binary) you launch this with.

  Without Accessibility the app still works: set Result to "Copy to
  clipboard" and bind a key to the whisper-dictate-local command in Raycast,
  Hammerspoon, Karabiner or an Automator Quick Action.

  Then:  whisper-dictate-local --setup
  and:   whisper-dictate-local-tray &
EOF
else
    echo "Done. Next:"
    echo "    whisper-dictate-local --setup    # model, engine and the shortcut"
    echo "    whisper-dictate-local-tray &"
fi
