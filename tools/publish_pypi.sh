#!/usr/bin/env bash
# Upload the current version to PyPI. Separate from make_release.sh on
# purpose: a GitHub release can be deleted and recut, a PyPI upload cannot.
# A version number is burned the moment it lands, even if you delete it.
#
#   ./tools/publish_pypi.sh --test     # rehearse on test.pypi.org
#   ./tools/publish_pypi.sh            # the real thing
#
# Needs an API token. Create one at https://pypi.org/manage/account/token/
# and either put it in ~/.pypirc or export it:
#
#   export TWINE_USERNAME=__token__
#   export TWINE_PASSWORD=pypi-AgEI...
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
say() { printf '  %s\n' "$*"; }

TEST=0
[ "${1:-}" = "--test" ] && TEST=1

VERSION="$(python3 -c "
import re, pathlib
print(re.search(r'^version = \"([^\"]+)\"',
                pathlib.Path('pyproject.toml').read_text(), re.M).group(1))")"
echo "Publishing whisper-dictate-local $VERSION to $([ "$TEST" = 1 ] && echo test.pypi.org || echo pypi.org)"

python3 -m twine --version >/dev/null 2>&1 \
    || die "twine is not installed. Try: pipx install twine"

# -- build both artefacts ----------------------------------------------------
# An sdist as well as a wheel: the wheel is what almost everyone installs, but
# the sdist is what lets anyone rebuild the package from source, and PyPI is
# the only place most people will look for it.
rm -rf dist build ./*.egg-info
python3 -c "
import setuptools.build_meta as b
b.build_wheel('dist'); b.build_sdist('dist')" >/dev/null 2>&1
rm -rf build ./*.egg-info

WHL="dist/whisper_dictate_local-${VERSION}-py3-none-any.whl"
SDIST="dist/whisper-dictate-local-${VERSION}.tar.gz"
[ -f "$WHL" ] || die "expected $WHL"
[ -f "$SDIST" ] || die "expected $SDIST"
say "built $(basename "$WHL") and $(basename "$SDIST")"

# -- validate ----------------------------------------------------------------
# The README becomes the project page, and PyPI resolves nothing relative to
# the repository: a relative image or file link renders as a broken link there
# while looking perfect on GitHub.
python3 - <<'PY'
import re, pathlib, sys
text = pathlib.Path("README.md").read_text(encoding="utf-8")
relative = [t for t in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text)
            if not t.startswith(("#", "http"))]
if relative:
    sys.exit("relative links break on PyPI: " + ", ".join(relative))
PY
say "no relative links in the description"

python3 -m twine check dist/* || die "twine check failed"

# -- upload ------------------------------------------------------------------
if [ "$TEST" = "1" ]; then
    python3 -m twine upload --repository testpypi dist/*
    say "https://test.pypi.org/project/whisper-dictate-local/$VERSION/"
    say "rehearse the install with:"
    say "  pipx install --pip-args='--extra-index-url https://pypi.org/simple' \\"
    say "    --index-url https://test.pypi.org/simple/ whisper-dictate-local"
else
    printf '  About to upload %s to PyPI permanently. Continue? [y/N] ' "$VERSION"
    read -r reply
    [ "$reply" = "y" ] || [ "$reply" = "Y" ] || die "aborted"
    python3 -m twine upload dist/*
    say "https://pypi.org/project/whisper-dictate-local/$VERSION/"
fi
