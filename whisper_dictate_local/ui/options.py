"""Option lists shared by every front end.

Deliberately free of any toolkit import. The GTK dialog and the macOS menu bar
present these differently -- a combo box against a submenu -- but they must
offer the same choices and filter them by the same rules, or the two platforms
drift into disagreeing about what the app can do.
"""

from __future__ import annotations

from .. import backend

# A shortlist, not a limit: whisper knows around a hundred languages, and any
# of its codes can be set by hand in the config file.
LANGUAGES = [
    ("auto", "Auto-detect"),
    ("uk", "Ukrainian"),
    ("en", "English"),
    ("pl", "Polish"),
    ("de", "German"),
    ("es", "Spanish"),
    ("fr", "French"),
]

# (code, label, capabilities it needs)
ALL_OUTPUT_MODES = [
    ("type_and_copy", "Type it and copy to clipboard", ("type", "copy")),
    ("type", "Type it into the focused window", ("type",)),
    ("copy", "Copy to clipboard only", ("copy",)),
]


def output_modes() -> list[tuple[str, str]]:
    """Only the delivery modes this machine can actually carry out."""
    can = {"type": backend.TOOLS.can_type, "copy": backend.TOOLS.can_copy}
    return [(code, label) for code, label, needs in ALL_OUTPUT_MODES
            if all(can[n] for n in needs)]


def language_label(code: str) -> str:
    for c, label in LANGUAGES:
        if c == code:
            return label
    # A code set by hand in the config that is not on the shortlist.
    return code
