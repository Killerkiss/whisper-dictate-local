"""Transcript history.

Every transcript is kept in a small SQLite file, written before the app tries
to type or copy it. Typing leaves nothing behind once the keystrokes land --
in the wrong window, or nowhere if focus moved -- so without this a dictation
that missed its target is gone for good.

SQLite rather than a JSON or text file because two processes write it: the
tray, and the CLI toggle when no tray is running. SQLite serialises those
writes itself; appending to a shared file would need locking of our own.

Nothing here may break dictation. Every call logs and carries on when the
database cannot be opened or written, because losing a history entry is far
cheaper than losing the transcript that was about to be typed.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DATA_DIR = (
    Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    / "whisper-dictate-local"
)
DB_PATH = DATA_DIR / "history.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL    NOT NULL,
    text       TEXT    NOT NULL,
    language   TEXT    NOT NULL DEFAULT '',
    translated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS transcripts_created_at ON transcripts (created_at);
"""


@dataclass(frozen=True)
class Entry:
    id: int
    created_at: float
    text: str
    language: str
    translated: bool


@contextmanager
def _db(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open, commit on success, and always close.

    A connection per call rather than one held open: the tray runs for days,
    and a long-lived handle would keep a deleted or replaced file alive.
    """
    path = path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # A short busy timeout: the tray and the CLI never hold the lock for more
    # than a single insert, so waiting a little beats failing outright.
    conn = sqlite3.connect(str(path), timeout=2.0)
    try:
        conn.executescript(_SCHEMA)
        # SQLite's own lower() folds ASCII only, which would make searching
        # Ukrainian text case-sensitive.
        conn.create_function("py_casefold", 1,
                             lambda s: s.casefold() if isinstance(s, str) else s,
                             deterministic=True)
        # The transcripts are as private as anything the user types; keep the
        # file readable by them alone, whatever the umask says.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        yield conn
        conn.commit()
    finally:
        conn.close()


def add(text: str, language: str = "", translated: bool = False,
        path: Path | None = None) -> None:
    """Record one transcript. Never raises."""
    if not text:
        return
    try:
        with _db(path) as conn:
            conn.execute(
                "INSERT INTO transcripts (created_at, text, language, translated) "
                "VALUES (?, ?, ?, ?)",
                (time.time(), text, language, int(translated)),
            )
    except (sqlite3.Error, OSError) as exc:
        log.warning("could not save the transcript to history (%s)", exc)


def recent(limit: int = 500, query: str = "", path: Path | None = None) -> list[Entry]:
    """Newest first, optionally filtered to entries containing `query`."""
    sql = "SELECT id, created_at, text, language, translated FROM transcripts"
    args: list[object] = []
    if query:
        # instr() rather than LIKE: no wildcards to escape, and LIKE ignores
        # case for ASCII only.
        sql += " WHERE instr(py_casefold(text), ?) > 0"
        args.append(query.casefold())
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    args.append(int(limit))
    try:
        with _db(path) as conn:
            rows = conn.execute(sql, args).fetchall()
    except (sqlite3.Error, OSError) as exc:
        log.warning("could not read the history (%s)", exc)
        return []
    return [Entry(r[0], r[1], r[2], r[3], bool(r[4])) for r in rows]


def delete(entry_id: int, path: Path | None = None) -> None:
    try:
        with _db(path) as conn:
            conn.execute("DELETE FROM transcripts WHERE id = ?", (int(entry_id),))
    except (sqlite3.Error, OSError) as exc:
        log.warning("could not delete history entry %s (%s)", entry_id, exc)


def clear(path: Path | None = None) -> None:
    try:
        with _db(path) as conn:
            conn.execute("DELETE FROM transcripts")
            conn.commit()
            # DELETE leaves the old text in free pages of the file; VACUUM
            # rewrites it, so clearing actually removes the words from disk.
            # It cannot run inside a transaction, hence the commit first.
            conn.execute("VACUUM")
    except (sqlite3.Error, OSError) as exc:
        log.warning("could not clear the history (%s)", exc)
