#!/usr/bin/env bash
# Cut a release: set the version everywhere, build, verify, tag and publish.
#
#   ./tools/make_release.sh 0.4.0 docs/changelog/0.4.0.md
#   ./tools/make_release.sh 0.4.0 docs/changelog/0.4.0.md --dry-run
#
# The install instructions are generated from docs/release-install.md.in
# rather than written by hand, because every one of them contains the version
# number. Hand-edited, they go stale the moment a release is cut, and a wrong
# install command is the single worst thing to ship -- it fails for strangers
# on their first contact with the project, and nobody who already has it
# installed will notice.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

REPO="https://github.com/Killerkiss/whisper-dictate-local"
VERSION="${1:-}"
HIGHLIGHTS="${2:-}"
DRY_RUN=0
[ "${3:-}" = "--dry-run" ] && DRY_RUN=1

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
say() { printf '  %s\n' "$*"; }

[ -n "$VERSION" ] || die "usage: $0 <version> <highlights.md> [--dry-run]"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "version must look like 1.2.3"
[ -n "$HIGHLIGHTS" ] && [ -f "$HIGHLIGHTS" ] || die "need a highlights file describing what changed"

echo "Releasing v$VERSION"
[ "$DRY_RUN" = "1" ] && say "(dry run: nothing will be written, tagged or published)"

# -- preconditions -----------------------------------------------------------
command -v gh >/dev/null || die "gh is not installed"
if [ "$DRY_RUN" = "0" ]; then
    [ -z "$(git status --porcelain)" ] || die "working tree is dirty; commit first"
    git rev-parse "v$VERSION" >/dev/null 2>&1 && die "tag v$VERSION already exists"
fi

# -- version, in the two places it appears -----------------------------------
render_install() {
    sed -e "s|@VERSION@|$VERSION|g" -e "s|@REPO@|$REPO|g" \
        "$ROOT/docs/release-install.md.in"
}

if [ "$DRY_RUN" = "0" ]; then
    python3 - "$VERSION" <<'PY'
import re, sys
from pathlib import Path
version = sys.argv[1]

p = Path("pyproject.toml"); t = p.read_text(encoding="utf-8")
t, n = re.subn(r'^version = "[^"]+"', f'version = "{version}"', t, count=1, flags=re.M)
assert n == 1, "no version line in pyproject.toml"
p.write_text(t, encoding="utf-8")

# The README carries the same URLs, so it goes stale in exactly the same way.
r = Path("README.md"); m = r.read_text(encoding="utf-8")
m = re.sub(r"/releases/download/v\d+\.\d+\.\d+/", f"/releases/download/v{version}/", m)
m = re.sub(r"whisper-dictate-local_\d+\.\d+\.\d+_all\.deb",
           f"whisper-dictate-local_{version}_all.deb", m)
m = re.sub(r"whisper_dictate_local-\d+\.\d+\.\d+-py3-none-any\.whl",
           f"whisper_dictate_local-{version}-py3-none-any.whl", m)
r.write_text(m, encoding="utf-8")
print(f"  version set to {version} in pyproject.toml and README.md")
PY
else
    say "would set version to $VERSION in pyproject.toml and README.md"
fi

# -- build -------------------------------------------------------------------
# Skipped on a dry run: the artefacts are named after the version, and a dry
# run deliberately has not written the new version anywhere to build from.
# The point of the dry run is to read the notes before they go out.
if [ "$DRY_RUN" = "1" ]; then
    NOTES_ONLY="$(mktemp -d)"; trap 'rm -rf "$NOTES_ONLY"' EXIT
    { cat "$HIGHLIGHTS"; echo; render_install; } > "$NOTES_ONLY/notes.md"
    grep -qE '@[A-Z_]+@' "$NOTES_ONLY/notes.md" \
        && die "unsubstituted placeholder in the notes"
    echo
    echo "----- release notes that would be published -----"
    cat "$NOTES_ONLY/notes.md"
    echo "------------------------------------------------"
    exit 0
fi

rm -rf dist build ./*.egg-info
"$ROOT/tools/make_deb.sh" >/dev/null
# The sdist is not attached to the GitHub release, but building it here
# means a broken source build is caught now rather than at PyPI upload time.
python3 -c "
import setuptools.build_meta as b
b.build_wheel('dist'); b.build_sdist('dist')" >/dev/null 2>&1
rm -rf build ./*.egg-info
DEB="dist/whisper-dictate-local_${VERSION}_all.deb"
WHL="dist/whisper_dictate_local-${VERSION}-py3-none-any.whl"
[ -f "$DEB" ] || die "expected $DEB"
[ -f "$WHL" ] || die "expected $WHL"
say "built $(basename "$DEB") and $(basename "$WHL")"

# -- verify before publishing ------------------------------------------------
# A broken artefact found here costs a minute; found by a stranger it costs
# them their first impression.
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
dpkg-deb -x "$DEB" "$TMP/deb"
PYTHONPATH="$TMP/deb/usr/lib/python3/dist-packages" python3 -c "
import whisper_dictate_local, whisper_dictate_local.backend as b
assert hasattr(b, 'TOOLS')
" || die "the package does not import"
dpkg-deb --info "$DEB" | grep -q "Version: $VERSION" || die "the .deb reports the wrong version"
python3 - "$WHL" "$VERSION" <<'PY'
import sys, zipfile
names = zipfile.ZipFile(sys.argv[1]).namelist()
assert any(n.endswith("entry_points.txt") for n in names), "wheel has no entry points"
assert any("LICENSE" in n for n in names), "wheel carries no licence"
PY
say "artefacts verified"

# -- notes -------------------------------------------------------------------
NOTES="$TMP/notes.md"
{ cat "$HIGHLIGHTS"; echo; render_install; } > "$NOTES"
grep -qE '@[A-Z_]+@' "$NOTES" && die "unsubstituted placeholder in the notes"

# -- publish -----------------------------------------------------------------
git add -A
git commit -q -m "Release v$VERSION"
git tag -a "v$VERSION" -m "v$VERSION"
git push -q origin HEAD
git push -q origin "v$VERSION"
gh release create "v$VERSION" "$DEB" "$WHL" \
    --title "v$VERSION" --notes-file "$NOTES" >/dev/null
say "published $REPO/releases/tag/v$VERSION"

# -- and check the published links actually work -----------------------------
for f in "$(basename "$DEB")" "$(basename "$WHL")"; do
    url="$REPO/releases/download/v$VERSION/$f"
    code="$(curl -s -o /dev/null -w '%{http_code}' -L "$url")"
    [ "$code" = "200" ] || die "published asset is not reachable: $url ($code)"
    say "reachable: $f"
done
