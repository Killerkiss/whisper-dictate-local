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
    [ "$removed" -gt 0 ] && say "removed $removed file(s) from the old uk-dictate install"
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
if [ "$PLATFORM" = "macos" ]; then
    AGENT_DIR="$HOME/Library/LaunchAgents"
    LOG="$HOME/Library/Logs/whisper-dictate-local.log"
    mkdir -p "$AGENT_DIR" "$(dirname "$LOG")"
    PLIST="$AGENT_DIR/local.whisper-dictate-local.plist"

    sed -e "s|@BIN_DIR@|$BIN_DIR|g" -e "s|@LOG@|$LOG|g" \
        "$ROOT/launchd/local.whisper-dictate-local.plist.in" > "$PLIST"
    # bootout first so a re-run picks up an edited plist rather than keeping
    # whatever was loaded at login.
    launchctl bootout "gui/$(id -u)/local.whisper-dictate-local" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null \
        || launchctl load -w "$PLIST" 2>/dev/null \
        || say "NOTE: could not load the launch agent; it will start at next login"
    say "menu bar app autostarts at login (log: $LOG)"
else
    ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
    APPS_DIR="$HOME/.local/share/applications"
    AUTOSTART_DIR="$HOME/.config/autostart"
    mkdir -p "$ICON_DIR" "$APPS_DIR" "$AUTOSTART_DIR"

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

    # Autostart reuses the same file, so the two can never drift apart.
    cp "$APPS_DIR/whisper-dictate-local-tray.desktop" \
       "$AUTOSTART_DIR/whisper-dictate-local-tray.desktop"
    say "tray autostart installed"

    command -v update-desktop-database >/dev/null \
        && update-desktop-database "$APPS_DIR" 2>/dev/null || true

    # -- keyboard shortcut ---------------------------------------------------
    if command -v gsettings >/dev/null \
       && gsettings list-schemas 2>/dev/null | grep -q org.cinnamon.desktop.keybindings; then
        KEY="${DICTATE_KEY:-F8}"
        P="/org/cinnamon/desktop/keybindings/custom-keybindings/custom0/"
        EXISTING="$(gsettings get org.cinnamon.desktop.keybindings custom-list)"
        if [ "$EXISTING" = "@as []" ]; then
            gsettings set org.cinnamon.desktop.keybindings custom-list "['custom0']"
        fi
        gsettings set "org.cinnamon.desktop.keybindings.custom-keybinding:$P" name "Dictation (toggle)"
        gsettings set "org.cinnamon.desktop.keybindings.custom-keybinding:$P" command "$BIN_DIR/whisper-dictate-local"
        gsettings set "org.cinnamon.desktop.keybindings.custom-keybinding:$P" binding "['$KEY']"
        say "shortcut bound to $KEY"
    fi
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

  Start it now with:  whisper-dictate-local-tray &
  Check what was detected:  whisper-dictate-local --check
EOF
else
    echo "Done. Start the tray now with:  whisper-dictate-local-tray &"
    echo "Check what was detected with:  whisper-dictate-local --check"
fi
