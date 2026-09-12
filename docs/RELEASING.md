# Releasing

```bash
# 1. write what changed, in the user's terms
$EDITOR docs/changelog/0.4.0.md

# 2. read the notes before anyone else does
./tools/make_release.sh 0.4.0 docs/changelog/0.4.0.md --dry-run

# 3. cut it
./tools/make_release.sh 0.4.0 docs/changelog/0.4.0.md
```

That is the whole process. The script sets the version, builds both artefacts,
verifies them, publishes, and then checks the published links resolve.

## Why the install instructions are generated

Every install command contains the version number — twice in the `.deb` line
alone. Written by hand in the release notes, they are correct for exactly one
release and wrong for every one after, and **nobody who already has the app
installed will ever notice**. The people who hit a broken install command are
strangers, on their first contact with the project, with no reason to report
it rather than close the tab.

So `docs/release-install.md.in` is the single source, `@VERSION@` and `@REPO@`
are substituted at release time, and the script refuses to publish if any
`@PLACEHOLDER@` survives. The README carries the same URLs and is rewritten by
the same step, so the two cannot disagree.

Add a new install route by editing the template, not the release notes.

## What the changelog file should say

Only what changed, in the terms a user would use — the install section is
appended automatically, so do not repeat it. Lead with whatever most affects
someone already running the app.

Worth stating plainly:

- anything that stops working, or needs a manual step after upgrading
- new defaults, and whether an existing config is left alone
- **what is still unverified.** macOS has never been run on a Mac; saying so
  in every release is the difference between a known limitation and a broken
  promise

## What the script checks before publishing

A broken artefact caught here costs a minute. Caught by a stranger, it costs
them their first impression of the project.

- the working tree is clean and the tag does not already exist
- the version is `x.y.z`, and a changelog file was actually passed
- the `.deb` extracts, imports, and reports the version it claims
- the wheel carries entry points and the licence
- no `@PLACEHOLDER@` survived into the notes
- after publishing, both asset URLs return 200

## Version numbers

Ordinary semver, judged from the user's side: a fix that changes no behaviour
is a patch, a new setting or a platform is a minor, anything that invalidates
an existing config or install is a major.

## Manual steps that remain

- `python3 tools/make_screenshots.py` after any settings UI change. The
  screenshots are generated from `DEFAULTS`, never from the developer's own
  config, so they cannot leak a home directory or a personal vocabulary hint.
- Publishing to PyPI, if that is ever wanted. It is not needed for any command
  in the install template: `pipx` installs happily from a release URL.
