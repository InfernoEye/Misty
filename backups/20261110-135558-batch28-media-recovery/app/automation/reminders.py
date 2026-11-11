"""Isolated, local reminder store for Misty.

This module is deliberately self-contained.  It imports no Qt, model, network,
subprocess or shell component, and writes only to its own SQLite database, so
it can be reviewed and tested before it is wired into the app.

Reminder text is user content.  It is stored locally because it *is* the
record, but it is never logged, never echoed in an exception message, and
never included in ``repr`` output.  Failures are reported with a stable error
code and a fixed message; raw SQLite or OS exception text never escapes.

Storage
-------
``reminders(id, text, due_at, created_at, status, fired_at)``

``due_at`` and ``created_at`` are canonical UTC ISO-8601 strings with
microseconds and a ``Z`` suffix.  The fixed-width format means lexicographic
ordering matches chronological ordering, so ``ORDER BY due_at`` is enough.
``status`` is one of ``pending``, ``fired`` or ``cancelled``.

Concurrency
-----------
The database uses WAL, a busy timeout and one short-lived connection per
operation.  Mutations are additionally serialized with a process-wide lock,
so a single :class:`ReminderStore` instance is safe to share between threads.

Public API
----------
``add(text, due_at)``
    Validate and insert a pending reminder; returns id/text/due_at/status.
``list(status=None, limit=100)``
    Reminders ordered by ``due_at`` (then id), optionally filtered.
``due(now=None, limit=100)``
    Pending reminders whose ``due_at`` is at or before ``now``.
``mark_fired(id, now=None)``
    Move a pending reminder to ``fired``; safe to call repeatedly.
``cancel(id)``
    Move a pending reminder to ``cancelled``; safe to call repeatedly.
``counts()``
    Totals per status, always with all three keys.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_HORIZON",
    "MAX_LIMIT",
    "MAX_TEXT_LENGTH",
    "ReminderError",
    "ReminderStore",
    "ReminderStoreError",
    "STATUSES",
]

MAX_TEXT_LENGTH = 500
MAX_DUE_LENGTH = 64
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
MAX_HORIZON = timedelta(days=365)

STATUSES = ("pending", "fired", "cancelled")

# C0 controls, DEL and C1 controls are all rejected: a reminder is rendered as
# a single line in a popup or tray notification.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    due_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'fired', 'cancelled')),
    fired_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_reminders_due
ON reminders (status, due_at);
"""

_SELECT_COLUMNS = "id, text, due_at, created_at, status, fired_at"


class ReminderError(ValueError):
    """Raised for invalid reminder input.

    ``code`` is a stable identifier for callers; ``str(error)`` is a fixed
    sentence that never contains the rejected reminder text.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ReminderStoreError(RuntimeError):
    """Raised when the reminder database cannot be opened or used."""


def _best_effort_chmod(path: Path, mode: int) -> None:
    if os.name != "posix":
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _format_utc(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _validate_text(value: object) -> str:
    if not isinstance(value, str):
        raise ReminderError(
            "invalid_text", "reminder text must be a string"
        )
    if len(value) > MAX_TEXT_LENGTH:
        raise ReminderError(
            "text_too_long",
            f"reminder text must be at most {MAX_TEXT_LENGTH} characters",
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ReminderError(
            "invalid_text", "reminder text must be valid Unicode"
        ) from None
    if _CONTROL_RE.search(value):
        raise ReminderError(
            "invalid_text", "reminder text must not contain control characters"
        )
    text = value.strip()
    if not text:
        raise ReminderError(
            "invalid_text", "reminder text must not be empty"
        )
    return text


def _parse_due(value: object, now: datetime) -> str:
    if not isinstance(value, str):
        raise ReminderError(
            "invalid_due_at", "due_at must be an ISO-8601 string"
        )
    raw = value.strip()
    if not raw or len(raw) > MAX_DUE_LENGTH:
        raise ReminderError(
            "invalid_due_at", "due_at must be a short ISO-8601 string"
        )
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        raise ReminderError(
            "invalid_due_at", "due_at must be ISO-8601 with a timezone"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReminderError(
            "invalid_due_at", "due_at must include a timezone offset"
        )
    try:
        due = parsed.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ReminderError(
            "invalid_due_at", "due_at must be a valid UTC instant"
        ) from None
    if due <= now:
        raise ReminderError("due_in_past", "due_at must be in the future")
    if due - now > MAX_HORIZON:
        raise ReminderError(
            "due_too_far", "due_at must be within one year"
        )
    return _format_utc(due)


def _coerce_now(value: object) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise ReminderError(
            "invalid_now", "now must be a timezone-aware datetime"
        )
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReminderError(
            "invalid_now", "now must be a timezone-aware datetime"
        )
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ReminderError(
            "invalid_now", "now must be a valid UTC instant"
        ) from None


def _validate_reminder_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReminderError(
            "invalid_id", "reminder id must be an integer"
        )
    if value <= 0:
        raise ReminderError(
            "invalid_id", "reminder id must be a positive integer"
        )
    return value


def _validate_limit(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_LIMIT
    ):
        raise ReminderError(
            "invalid_limit",
            f"limit must be an integer between 1 and {MAX_LIMIT}",
        )
    return value


def _validate_status_filter(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReminderError(
            "invalid_status",
            "status must be pending, fired, cancelled or None",
        )
    status = value.strip().lower()
    if status not in STATUSES:
        raise ReminderError(
            "invalid_status",
            "status must be pending, fired, cancelled or None",
        )
    return status


def _row_to_reminder(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "text": row["text"],
        "due_at": row["due_at"],
        "created_at": row["created_at"],
        "status": row["status"],
        "fired_at": row["fired_at"],
    }


class ReminderStore:
    """Thread-safe local reminder store backed by one SQLite file.

    Construction creates the parent directory and the schema if needed.  The
    object holds configuration and a lock only; each operation opens its own
    connection, so instances may be shared freely between threads.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = self._coerce_path(path)
        self._lock = threading.Lock()

        parent = self.path.parent
        try:
            created = not parent.exists()
            parent.mkdir(parents=True, exist_ok=True)
            if created:
                _best_effort_chmod(parent, 0o700)
        except OSError:
            raise ReminderStoreError(
                "reminder directory could not be created"
            ) from None

        try:
            self._initialize()
        except sqlite3.Error:
            raise ReminderStoreError(
                "reminder database could not be initialized"
            ) from None
        self._restrict_permissions()

    def __repr__(self) -> str:
        # Deliberately excludes every stored field; reminder text is content.
        return f"ReminderStore(path={str(self.path)!r})"

    def __enter__(self) -> "ReminderStore":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        """Release resources.

        Connections are opened per operation, so there is nothing persistent
        to close.  The method exists so the store can be used as a managed
        resource without special-casing it, and is safe to call repeatedly.
        """

    # -- construction helpers -------------------------------------------

    @staticmethod
    def _coerce_path(value: object) -> Path:
        if isinstance(value, (str, os.PathLike)):
            try:
                raw = os.fspath(value)
            except TypeError:
                raw = None
            if isinstance(raw, str) and raw.strip() and "\x00" not in raw:
                try:
                    return Path(raw).expanduser()
                except (OSError, RuntimeError, ValueError):
                    pass
        raise ReminderStoreError(
            "a usable reminder database path is required"
        )

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = NORMAL")
            except sqlite3.Error:
                pass
            connection.executescript(_SCHEMA)

    def _restrict_permissions(self) -> None:
        _best_effort_chmod(self.path, 0o600)
        for suffix in ("-wal", "-shm"):
            companion = Path(f"{self.path}{suffix}")
            if companion.exists():
                _best_effort_chmod(companion, 0o600)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 10000")
            with connection:
                yield connection
        finally:
            connection.close()

    # -- writes ---------------------------------------------------------

    def add(self, text: str, due_at: str) -> dict[str, Any]:
        """Insert a pending reminder and return id/text/due_at/status.

        ``due_at`` must be an ISO-8601 string with an explicit timezone; it is
        normalized to UTC.  It must lie in the future and within one year.
        """

        now = datetime.now(timezone.utc)
        clean_text = _validate_text(text)
        normalized_due = _parse_due(due_at, now)
        created_at = _format_utc(now)

        try:
            with self._lock, self._connection() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO reminders
                        (text, due_at, created_at, status, fired_at)
                    VALUES (?, ?, ?, 'pending', NULL)
                    """,
                    (clean_text, normalized_due, created_at),
                )
                reminder_id = int(cursor.lastrowid)
        except sqlite3.Error:
            raise ReminderStoreError(
                "reminder storage is unavailable"
            ) from None

        return {
            "id": reminder_id,
            "text": clean_text,
            "due_at": normalized_due,
            "status": "pending",
        }

    def mark_fired(
        self, id: int, now: datetime | None = None
    ) -> dict[str, Any] | None:
        """Mark a pending reminder fired, returning the updated row.

        Only ``pending`` rows change.  The row is returned only when *this*
        call performed the transition; an already fired, already cancelled or
        missing reminder returns ``None`` and is left untouched.  That makes
        repeated calls (including concurrent ones) safe and lets a caller use
        the truthy return value to send exactly one notification.
        """

        reminder_id = _validate_reminder_id(id)
        timestamp = _format_utc(_coerce_now(now))

        try:
            with self._lock, self._connection() as connection:
                cursor = connection.execute(
                    """
                    UPDATE reminders
                    SET status = 'fired', fired_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (timestamp, reminder_id),
                )
                if cursor.rowcount != 1:
                    return None
                row = connection.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM reminders WHERE id = ?",
                    (reminder_id,),
                ).fetchone()
        except sqlite3.Error:
            raise ReminderStoreError(
                "reminder storage is unavailable"
            ) from None

        return _row_to_reminder(row) if row is not None else None

    def cancel(self, id: int) -> dict[str, Any] | None:
        """Cancel a pending reminder, returning the updated row.

        Only ``pending`` rows change.  The row is returned only when *this*
        call performed the transition; a fired, already cancelled or missing
        reminder returns ``None`` and is left untouched, so repeated calls are
        safe.
        """

        reminder_id = _validate_reminder_id(id)

        try:
            with self._lock, self._connection() as connection:
                cursor = connection.execute(
                    """
                    UPDATE reminders
                    SET status = 'cancelled'
                    WHERE id = ? AND status = 'pending'
                    """,
                    (reminder_id,),
                )
                if cursor.rowcount != 1:
                    return None
                row = connection.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM reminders WHERE id = ?",
                    (reminder_id,),
                ).fetchone()
        except sqlite3.Error:
            raise ReminderStoreError(
                "reminder storage is unavailable"
            ) from None

        return _row_to_reminder(row) if row is not None else None

    # -- reads ----------------------------------------------------------

    def list(
        self, status: str | None = None, limit: int = DEFAULT_LIMIT
    ) -> list[dict[str, Any]]:
        """Return reminders ordered by ``due_at`` (then id).

        ``status`` is an optional exact filter and ``limit`` is bounded by
        :data:`MAX_LIMIT`.
        """

        normalized_status = _validate_status_filter(status)
        normalized_limit = _validate_limit(limit)

        sql = f"SELECT {_SELECT_COLUMNS} FROM reminders"
        parameters: list[Any] = []
        if normalized_status is not None:
            sql += " WHERE status = ?"
            parameters.append(normalized_status)
        sql += " ORDER BY due_at ASC, id ASC LIMIT ?"
        parameters.append(normalized_limit)

        try:
            with self._lock, self._connection() as connection:
                rows = connection.execute(
                    sql, tuple(parameters)
                ).fetchall()
        except sqlite3.Error:
            raise ReminderStoreError(
                "reminder storage is unavailable"
            ) from None

        return [_row_to_reminder(row) for row in rows]

    def due(
        self, now: datetime | None = None, limit: int = DEFAULT_LIMIT
    ) -> list[dict[str, Any]]:
        """Return pending reminders due at or before ``now``, earliest first."""

        reference = _format_utc(_coerce_now(now))
        normalized_limit = _validate_limit(limit)

        try:
            with self._lock, self._connection() as connection:
                rows = connection.execute(
                    f"""
                    SELECT {_SELECT_COLUMNS} FROM reminders
                    WHERE status = 'pending' AND due_at <= ?
                    ORDER BY due_at ASC, id ASC
                    LIMIT ?
                    """,
                    (reference, normalized_limit),
                ).fetchall()
        except sqlite3.Error:
            raise ReminderStoreError(
                "reminder storage is unavailable"
            ) from None

        return [_row_to_reminder(row) for row in rows]

    def counts(self) -> dict[str, int]:
        """Return the number of reminders in each status."""

        result = {status: 0 for status in STATUSES}
        try:
            with self._lock, self._connection() as connection:
                rows = connection.execute(
                    "SELECT status, COUNT(*) AS total "
                    "FROM reminders GROUP BY status"
                ).fetchall()
        except sqlite3.Error:
            raise ReminderStoreError(
                "reminder storage is unavailable"
            ) from None

        for row in rows:
            status = row["status"]
            if status in result:
                result[status] = int(row["total"])
        return result
