#!/usr/bin/env bash
# Link the launchers into ~/.local/bin, install the speech-engine service and
# autostart the tray. Safe to re-run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
AUTOSTART_DIR="$HOME/.config/autostart"

WHISPER_BIN="${WHISPER_BIN:-$HOME/opt/whisper.cpp/build/bin/whisper-server}"
MODEL="${MODEL:-$HOME/opt/whisper.cpp/models/ggml-large-v3.bin}"

say() { printf '  %s\n' "$*"; }

# Warned about at the end rather than killed: quitting it from its own menu
# lets it stop the speech engine and release VRAM the way it expects to.
pgrep -f 'whisper_dictate_local.tray|ukdictate.tray|uk-dictate-tray' >/dev/null 2>&1 \
    && LEGACY_TRAY_RUNNING=1 || true

echo "Installing whisper-dictate-local from $ROOT"

mkdir -p "$BIN_DIR" "$UNIT_DIR" "$AUTOSTART_DIR"

# -- clean up the pre-rename install -----------------------------------------
# The project was called uk-dictate. Its autostart entry points at a launcher
# that no longer exists, so leaving it behind means a failed app at every login
# and, worse, a stale menu entry that looks like the real thing. Settings are
# NOT touched here: the app migrates ~/.config/uk-dictate itself on first run.
LEGACY_FILES=(
    "$AUTOSTART_DIR/uk-dictate-tray.desktop"
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
# Regenerated on demand with the configured lead-in, so not worth migrating.
rm -rf "$HOME/.local/share/uk-dictate"
[ "$removed" -gt 0 ] && say "removed $removed file(s) from the old uk-dictate install"

ln -sf "$ROOT/bin/whisper-dictate-local"      "$BIN_DIR/whisper-dictate-local"
ln -sf "$ROOT/bin/whisper-dictate-local-tray" "$BIN_DIR/whisper-dictate-local-tray"
# Kept so an existing keyboard shortcut bound to `uk-dictate` still works.
# Drop it once you have repointed the shortcut at the new command.
ln -sf "$ROOT/bin/whisper-dictate-local"      "$BIN_DIR/uk-dictate"
say "launchers -> $BIN_DIR (with a uk-dictate compatibility link)"

# -- speech engine service ---------------------------------------------------
# Without systemd there is no unit to install: the app starts whisper-server
# itself as a child process instead. Everything else below still applies.
if ! command -v systemctl >/dev/null; then
    say "no systemd; the app will start the speech engine itself"
    if [ ! -x "$WHISPER_BIN" ]; then
        say "WARNING: set whisper_server in the config - $WHISPER_BIN is not executable"
    fi
elif [ -x "$WHISPER_BIN" ] && [ -f "$MODEL" ]; then
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

# -- icons -------------------------------------------------------------------
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$ICON_DIR"
cp "$ROOT/icons/whisper-dictate-local-recording.svg" "$ICON_DIR/"
cp "$ROOT/icons/whisper-dictate-local-busy.svg"      "$ICON_DIR/"
cp "$ROOT/icons/whisper-dictate-local-idle.svg"      "$ICON_DIR/whisper-dictate-local-idle-symbolic.svg"
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
say "icons installed"

# -- menu entry + autostart --------------------------------------------------
APPS_DIR="$HOME/.local/share/applications"
mkdir -p "$APPS_DIR"

sed "s|@BIN_DIR@|$BIN_DIR|g" "$ROOT/desktop/whisper-dictate-local-tray.desktop.in" \
    > "$APPS_DIR/whisper-dictate-local-tray.desktop"
chmod +x "$APPS_DIR/whisper-dictate-local-tray.desktop"
say "menu entry -> $APPS_DIR (search the menu for \"Dictation\")"

# Autostart reuses the same file, so the two can never drift apart.
cp "$APPS_DIR/whisper-dictate-local-tray.desktop" "$AUTOSTART_DIR/whisper-dictate-local-tray.desktop"
say "tray autostart installed"

if command -v update-desktop-database >/dev/null; then
    update-desktop-database "$APPS_DIR" 2>/dev/null || true
fi

# -- keyboard shortcut -------------------------------------------------------
if command -v gsettings >/dev/null && gsettings list-schemas | grep -q org.cinnamon.desktop.keybindings; then
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

echo
echo "Done. Start the tray now with:  whisper-dictate-local-tray &"
if [ -n "${LEGACY_TRAY_RUNNING:-}" ]; then
    echo "A uk-dictate tray is still running; quit it from its panel menu first."
fi
