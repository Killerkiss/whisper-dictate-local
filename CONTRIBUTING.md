# Contributing

`master` is protected: it takes no direct pushes, and a pull request needs one
approving review from the maintainer before it can be merged.

```bash
git clone https://github.com/Killerkiss/whisper-dictate-local
cd whisper-dictate-local
git checkout -b what-you-are-changing

# ... make the change ...

git push origin what-you-are-changing
gh pr create --fill        # or open it on GitHub
```

## Before you open it

- **Say what you ran it on.** Session type and distribution decide more here
  than anything else. `whisper-dictate-local --check` prints exactly what is
  relevant, and pasting its output saves a round trip.
- **Keep OS-specific commands in `backend.py`.** The rest of the app is
  written in terms of intent — "type this", "start the engine" — and picks a
  tool by probing for it, never by checking a distribution name. A change that
  reaches for a command outside that module is usually a change that belongs
  inside it.
- **Front ends live in `ui/`.** `ui/gtk_*.py` for Linux and BSD,
  `ui/macos_*.py` for macOS, shared option lists in `ui/options.py` so the two
  cannot offer different choices.
- **Settings must degrade, not lie.** If a setting cannot work on the current
  system, the dialog either drops the choice or greys it out with the reason.
  Ask `backend.TOOLS` rather than probing for the tool again.
- **Regenerate screenshots** with `python3 tools/make_screenshots.py` if you
  changed the settings UI.

## Especially welcome

- **macOS reports of any kind.** The menu bar front end was written against
  the documented APIs and has never been run on a Mac. Every Cocoa and Quartz
  call in it is unverified.
- **BSD reports.** Expected to work; untested.
- **Wayland typing**, via `wtype` or `ydotool`.
- Distribution packages — an AUR `PKGBUILD`, an RPM spec.

## Review

One approval is required and stale reviews are dismissed when new commits
land, so a review approves the code that will actually be merged rather than
an earlier version of it. Conversations must be resolved before merge.

Releases are cut by the maintainer; see `docs/RELEASING.md`.
