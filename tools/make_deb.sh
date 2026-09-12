#!/usr/bin/env bash
# Build a .deb of whisper-dictate-local.
#
# The point of the package is dependency resolution: apt pulls in GTK, the app
# indicator, Keybinder, xdotool, xclip and PulseAudio, which is the part users
# get wrong when installing by hand. It does NOT ship whisper.cpp or a model --
# the model alone is 2.9 GiB, past what belongs in a package and past GitHub's
# per-asset limit -- so `whisper-dictate-local --setup` fetches those on first
# run, choosing them from the hardware it finds.
#
# Pure Python and architecture-independent, so one .deb covers every machine.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-$ROOT/dist}"

VERSION="$(python3 - <<'PY'
import re, pathlib
text = pathlib.Path("pyproject.toml").read_text(encoding="utf-8")
print(re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1))
PY
)"
PKG="whisper-dictate-local"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "Building $PKG $VERSION"

# -- layout ------------------------------------------------------------------
SITE="$STAGE/usr/lib/python3/dist-packages"
mkdir -p "$SITE" "$STAGE/usr/bin" "$STAGE/DEBIAN" \
         "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/icons/hicolor/scalable/apps" \
         "$STAGE/usr/share/doc/$PKG"

cp -r "$ROOT/whisper_dictate_local" "$SITE/"
find "$SITE" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

cp "$ROOT/whisper_dictate_local/icons/"*.svg \
   "$STAGE/usr/share/icons/hicolor/scalable/apps/"
# The panel expects the idle icon under its -symbolic name so the theme
# recolours it; the other two are full colour by design.
mv "$STAGE/usr/share/icons/hicolor/scalable/apps/$PKG-idle.svg" \
   "$STAGE/usr/share/icons/hicolor/scalable/apps/$PKG-idle-symbolic.svg"

sed "s|@BIN_DIR@|/usr/bin|g" "$ROOT/desktop/$PKG-tray.desktop.in" \
    > "$STAGE/usr/share/applications/$PKG-tray.desktop"

cat > "$STAGE/usr/bin/$PKG" <<'EOF'
#!/bin/sh
exec python3 -m whisper_dictate_local.cli "$@"
EOF
cat > "$STAGE/usr/bin/$PKG-tray" <<'EOF'
#!/bin/sh
exec python3 -c 'from whisper_dictate_local.ui import main; raise SystemExit(main())' "$@"
EOF
chmod 755 "$STAGE/usr/bin/$PKG" "$STAGE/usr/bin/$PKG-tray"

cp "$ROOT/README.md" "$STAGE/usr/share/doc/$PKG/"

# -- metadata ----------------------------------------------------------------
# Alternatives on the indicator because distributions disagree about which of
# the two typelibs they ship, and either satisfies the tray.
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Maintainer: Ruslan Kaihorodov <kaugorodov.ruslan@gmail.com>
Depends: python3 (>= 3.9), python3-requests, python3-gi,
 gir1.2-gtk-3.0, gir1.2-keybinder-3.0,
 gir1.2-ayatanaappindicator3-0.1 | gir1.2-appindicator3-0.1,
 xdotool, xclip, pulseaudio-utils
Recommends: python3-xlib
Suggests: wl-clipboard, ydotool, libnotify-bin
Homepage: https://github.com/Killerkiss/whisper-dictate-local
Description: Local push-to-talk dictation, speech to text without the cloud
 Speak, and the text appears in whatever window you were already typing in.
 Any language Whisper supports can be transcribed as spoken, or translated to
 English. Everything runs on the machine via whisper.cpp; no API keys, and no
 audio leaves the computer.
 .
 This package installs the application only. The speech engine and the model
 are fetched by "whisper-dictate-local --setup", which picks a model to suit
 the hardware it finds; the model is several gigabytes and cannot sensibly be
 packaged.
EOF

cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -f -t /usr/share/icons/hicolor || true
    fi
    cat <<'MSG'

whisper-dictate-local is installed. One step left:

    whisper-dictate-local --setup

It looks at your hardware, recommends a model, and downloads that and the
speech engine. Then start it from the menu ("Whisper Dictate"), or run:

    whisper-dictate-local-tray &

MSG
fi
exit 0
EOF
chmod 755 "$STAGE/DEBIAN/postinst"

cat > "$STAGE/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || true
    fi
fi
exit 0
EOF
chmod 755 "$STAGE/DEBIAN/postrm"

# -- permissions -------------------------------------------------------------
# Modes are inherited from the working tree, which is typically group-writable.
# A system package must not be: anyone in the group could edit code that every
# user on the machine executes.
find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE" -type f -exec chmod 644 {} +
chmod 755 "$STAGE/usr/bin/$PKG" "$STAGE/usr/bin/$PKG-tray" \
          "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/postrm"

# -- build -------------------------------------------------------------------
mkdir -p "$OUT_DIR"
DEB="$OUT_DIR/${PKG}_${VERSION}_all.deb"
# Reproducible-ish: root-owned files, regardless of who builds.
dpkg-deb --root-owner-group --build "$STAGE" "$DEB" >/dev/null

echo "  $DEB  ($(du -h "$DEB" | cut -f1))"
