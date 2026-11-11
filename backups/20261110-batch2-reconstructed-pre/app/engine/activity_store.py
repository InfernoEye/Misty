"""Persistent, local activity history for Misty."""

import sqlite3
import threading
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATABASE = ROOT / "data" / "activity.sqlite3"


class ActivityStore:
    def __init__(self, database=DATABASE):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._lock, self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'info'
                )
            """)

    def add_event(self, event):
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")

        with self._lock, self._connect() as connection:
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
                    str(event.get("details", "")),
                    str(event.get("status", "info")),
                ),
            )

    def recent_events(self, limit=100):
        with self._lock, self._connect() as connection:
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
