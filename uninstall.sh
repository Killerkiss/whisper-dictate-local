#!/usr/bin/env bash
# Remove what install.sh put in place. Safe to re-run.
#
# By default this removes the application only. Settings and the speech model
# are left alone -- a model is gigabytes and a re-download is not something to
# trigger by accident -- and `--purge` removes settings and cached sounds too.
# whisper.cpp itself is never touched: you installed it, so you remove it.
set -euo pipefail

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

BIN_DIR="$HOME/.local/bin"
say() { printf '  %s\n' "$*"; }

case "$(uname -s)" in
    Darwin)           PLATFORM=macos ;;
    *BSD | DragonFly) PLATFORM=bsd ;;
    *)                PLATFORM=linux ;;
esac

echo "Removing whisper-dictate-local  ($PLATFORM)"

# -- stop it first -----------------------------------------------------------
# Before the files go, so the tray can shut the engine down itself and release
# the VRAM it is holding.
if pgrep -f 'whisper_dictate_local.ui|whisper_dictate_local.tray' >/dev/null 2>&1; then
    pkill -f 'whisper_dictate_local.ui|whisper_dictate_local.tray' || true
    sleep 1
    say "stopped the tray"
fi

if command -v systemctl >/dev/null && systemctl --user cat whisper-server.service >/dev/null 2>&1; then
    systemctl --user stop whisper-server.service 2>/dev/null || true
    systemctl --user disable whisper-server.service >/dev/null 2>&1 || true
    rm -f "$HOME/.config/systemd/user/whisper-server.service"
    systemctl --user daemon-reload
    say "removed the speech engine service"
fi

# Any engine this app started as a plain child process.
PIDFILE="${XDG_RUNTIME_DIR:-/tmp}/whisper-dictate-local/engine.pid"
if [ -f "$PIDFILE" ]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    rm -f "$PIDFILE"
fi

# -- files -------------------------------------------------------------------
FILES=(
    "$BIN_DIR/whisper-dictate-local"
    "$BIN_DIR/whisper-dictate-local-tray"
    "$BIN_DIR/uk-dictate"
    "$HOME/.local/share/applications/whisper-dictate-local-tray.desktop"
    "$HOME/.config/autostart/whisper-dictate-local-tray.desktop"
    "$HOME/.local/share/icons/hicolor/scalable/apps/whisper-dictate-local.svg"
    "$HOME/.local/share/icons/hicolor/scalable/apps/whisper-dictate-local-idle-symbolic.svg"
    "$HOME/.local/share/icons/hicolor/scalable/apps/whisper-dictate-local-recording.svg"
    "$HOME/.local/share/icons/hicolor/scalable/apps/whisper-dictate-local-busy.svg"
    "$HOME/Library/LaunchAgents/local.whisper-dictate-local.plist"
)
removed=0
for f in "${FILES[@]}"; do
    if [ -e "$f" ] || [ -L "$f" ]; then rm -f "$f"; removed=$((removed + 1)); fi
done
say "removed $removed file(s)"

if [ "$PLATFORM" = "macos" ]; then
    launchctl bootout "gui/$(id -u)/local.whisper-dictate-local" 2>/dev/null \
        || launchctl unload "$HOME/Library/LaunchAgents/local.whisper-dictate-local.plist" 2>/dev/null \
        || true
else
    command -v update-desktop-database >/dev/null \
        && update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
    command -v gtk-update-icon-cache >/dev/null \
        && gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

    # The shortcut points at a command that no longer exists; leaving it bound
    # means a key that silently does nothing.
    P="/org/cinnamon/desktop/keybindings/custom-keybindings/custom0/"
    SCHEMA="org.cinnamon.desktop.keybindings.custom-keybinding:$P"
    if command -v gsettings >/dev/null \
       && gsettings list-schemas 2>/dev/null | grep -q org.cinnamon.desktop.keybindings \
       && gsettings get "$SCHEMA" command 2>/dev/null | grep -q 'dictate'; then
        gsettings reset-recursively "$SCHEMA" 2>/dev/null || true
        gsettings set org.cinnamon.desktop.keybindings custom-list "@as []" 2>/dev/null || true
        say "released the keyboard shortcut"
    fi
fi

# -- settings and cached data ------------------------------------------------
if [ "$PURGE" = "1" ]; then
    rm -rf "$HOME/.config/whisper-dictate-local" \
           "$HOME/.local/share/whisper-dictate-local" \
           "${XDG_RUNTIME_DIR:-/tmp}/whisper-dictate-local"
    say "purged settings, downloaded models and cached sounds"
else
    say "kept settings in ~/.config/whisper-dictate-local  (--purge removes them)"
    [ -d "$HOME/.local/share/whisper-dictate-local" ] \
        && say "kept downloads in ~/.local/share/whisper-dictate-local"
fi

echo
if dpkg -l whisper-dictate-local >/dev/null 2>&1; then
    echo "A packaged copy is also installed. Remove it with:"
    echo "    sudo apt remove whisper-dictate-local"
fi
if python3 -c 'import whisper_dictate_local' 2>/dev/null; then
    echo "The Python package is still importable (pip/pipx install, or this checkout)."
    echo "    pipx uninstall whisper-dictate-local   # if you installed it that way"
fi
echo "whisper.cpp and your models were not touched."
