"""Persistent, local activity history for Misty.

Privacy and retention rules
---------------------------
* An event is stored with redacted details when its ``sensitive`` flag is
  truthy (``True``, ``1``, a non-empty string, ...) or when its ``answer`` or
  ``details`` field is itself flagged as sensitive (``answer_sensitive`` /
  ``details_sensitive``). Redaction drops the details text entirely while
  keeping the title and status so the history stays readable.
* ``retention_days`` bounds how much history the local database keeps. When it
  is set, stale rows are purged opportunistically after each insert and on
  construction, in bounded chunks, so the database cannot grow forever.
* ``purge(older_than_days=...)`` and ``clear()`` are the explicit retention
  API (used by tools/UI); they delete rows only, never anything outside the
  database file.

Every connection is closed deterministically: this module never relies on
``sqlite3.Connection.__enter__``, which commits but does not close.
"""

import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATABASE = ROOT / "data" / "activity.sqlite3"

REDACTED_DETAILS = "[details redacted from local activity history]"
MAX_DETAILS_LENGTH = 20_000
DEFAULT_RETENTION_DAYS = 30
PURGE_CHUNK = 500
MAX_RETENTION_DAYS = 3650


def utc_timestamp(moment=None):
    """Return a sortable UTC ISO-8601 timestamp for ``moment``.

    UTC is used on purpose: it has a zero, fixed offset, so plain string
    comparison orders timestamps correctly (a local offset such as ``+02:00``
    would break ``timestamp < cutoff`` comparisons).
    """
    if moment is None:
        moment = datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _event_is_sensitive(event):
    """Return whether any privacy flag on ``event`` demands redaction."""
    if event.get("sensitive"):
        return True
    for flag in ("answer_sensitive", "details_sensitive"):
        if event.get(flag):
            return True
    details = event.get("details")
    if isinstance(details, dict) and details.get("sensitive"):
        return True
    return False


def _validate_days(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Retention must be a number of days or None.")
    if value < 0 or value > MAX_RETENTION_DAYS:
        raise ValueError(
            f"Retention must be between 0 and {MAX_RETENTION_DAYS} days."
        )
    return float(value)


class ActivityStore:
    def __init__(self, database=DATABASE, retention_days=None, clock=None):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._clock = clock or (lambda: datetime.now().astimezone())
        self.retention_days = _validate_days(retention_days)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _now(self):
        return self._clock().astimezone()

    def _initialize(self):
        with self._lock, closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS activity (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        title TEXT NOT NULL,
                        details TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'info'
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS activity_timestamp_idx "
                    "ON activity(timestamp)"
                )
            if self.retention_days is not None:
                self._purge_locked(connection, self.retention_days)

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def add_event(self, event):
        if not isinstance(event, dict):
            raise TypeError("Activity events must be dictionaries")
        timestamp = utc_timestamp(self._now())
        details = str(event.get("details", "") or "")

        if _event_is_sensitive(event):
            details = REDACTED_DETAILS
        elif len(details) > MAX_DETAILS_LENGTH:
            details = details[:MAX_DETAILS_LENGTH] + "\n[truncated]"

        with self._lock, closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO activity
                        (timestamp, kind, title, details, status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        timestamp,
                        str(event.get("kind", "event")),
                        str(event.get("title", "Activity")),
                        details,
                        str(event.get("status", "info")),
                    ),
                )
            if self.retention_days is not None:
                self._purge_locked(connection, self.retention_days)
        return timestamp

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def recent_events(self, limit=100):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, 10_000)
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT timestamp, kind, title, details, status
                FROM activity
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()

        return [dict(row) for row in rows]

    def count(self):
        """Return the number of stored activity rows."""
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM activity"
            ).fetchone()
        return int(row["total"])

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    def purge(self, older_than_days=None):
        """Delete activity rows older than ``older_than_days``.

        ``None`` falls back to the configured :attr:`retention_days`; when
        neither is set, nothing is deleted. Returns the number of rows
        deleted. Deletion runs in bounded chunks so a huge history never
        blocks in one statement.
        """
        days = _validate_days(older_than_days)
        if days is None:
            days = self.retention_days
        if days is None:
            return 0
        with self._lock, closing(self._connect()) as connection:
            return self._purge_locked(connection, days)

    def clear(self):
        """Delete every stored activity row. Returns the number deleted."""
        with self._lock, closing(self._connect()) as connection:
            deleted = 0
            while True:
                with connection:
                    cursor = connection.execute(
                        "DELETE FROM activity WHERE id IN "
                        "(SELECT id FROM activity ORDER BY id LIMIT ?)",
                        (PURGE_CHUNK,),
                    )
                changed = max(int(cursor.rowcount or 0), 0)
                deleted += changed
                if changed < PURGE_CHUNK:
                    break
        return deleted

    def _purge_locked(self, connection, days):
        cutoff = utc_timestamp(self._now() - timedelta(days=days))
        deleted = 0
        while True:
            with connection:
                cursor = connection.execute(
                    "DELETE FROM activity WHERE id IN "
                    "(SELECT id FROM activity "
                    " WHERE timestamp < ? ORDER BY id LIMIT ?)",
                    (cutoff, PURGE_CHUNK),
                )
            changed = max(int(cursor.rowcount or 0), 0)
            deleted += changed
            if changed < PURGE_CHUNK:
                break
        return deleted
