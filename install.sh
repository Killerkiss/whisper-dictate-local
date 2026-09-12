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

echo "Installing uk-dictate from $ROOT"

mkdir -p "$BIN_DIR" "$UNIT_DIR" "$AUTOSTART_DIR"

ln -sf "$ROOT/bin/uk-dictate"      "$BIN_DIR/uk-dictate"
ln -sf "$ROOT/bin/uk-dictate-tray" "$BIN_DIR/uk-dictate-tray"
say "launchers -> $BIN_DIR"

# -- speech engine service ---------------------------------------------------
if [ -x "$WHISPER_BIN" ] && [ -f "$MODEL" ]; then
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
cp "$ROOT/icons/uk-dictate-recording.svg" "$ICON_DIR/"
cp "$ROOT/icons/uk-dictate-busy.svg"      "$ICON_DIR/"
cp "$ROOT/icons/uk-dictate-idle.svg"      "$ICON_DIR/uk-dictate-idle-symbolic.svg"
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
say "icons installed"

# -- menu entry + autostart --------------------------------------------------
APPS_DIR="$HOME/.local/share/applications"
mkdir -p "$APPS_DIR"

sed "s|@BIN_DIR@|$BIN_DIR|g" "$ROOT/desktop/uk-dictate-tray.desktop.in" \
    > "$APPS_DIR/uk-dictate-tray.desktop"
chmod +x "$APPS_DIR/uk-dictate-tray.desktop"
say "menu entry -> $APPS_DIR (search the menu for \"Dictation\")"

# Autostart reuses the same file, so the two can never drift apart.
cp "$APPS_DIR/uk-dictate-tray.desktop" "$AUTOSTART_DIR/uk-dictate-tray.desktop"
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
    gsettings set "org.cinnamon.desktop.keybindings.custom-keybinding:$P" command "$BIN_DIR/uk-dictate"
    gsettings set "org.cinnamon.desktop.keybindings.custom-keybinding:$P" binding "['$KEY']"
    say "shortcut bound to $KEY"
fi

echo
echo "Done. Start the tray now with:  uk-dictate-tray &"
