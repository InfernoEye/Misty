"""Private, local persistent memory for Misty."""

import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from contextlib import closing


ROOT = Path(__file__).resolve().parents[2]
DATABASE = ROOT / "data" / "memory.sqlite3"

CATEGORIES = frozenset({
    "preference",
    "fact",
    "project",
    "instruction",
})

MAX_CONTENT_LENGTH = 4000
MAX_QUERY_LENGTH = 500


def _timestamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class MemoryStore:
    """Thread-safe SQLite memory store with local full-text search."""

    def __init__(self, database=DATABASE):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(
            self.database,
            timeout=10,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self):
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(category IN (
                        'preference',
                        'fact',
                        'project',
                        'instruction'
                    ))
                )
            """)

            connection.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(
                    content,
                    category UNINDEXED,
                    content='memories',
                    content_rowid='id',
                    tokenize='unicode61 remove_diacritics 2'
                )
            """)

            connection.executescript("""
                CREATE TRIGGER IF NOT EXISTS memories_ai
                AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, content, category)
                    VALUES (new.id, new.content, new.category);
                END;

                CREATE TRIGGER IF NOT EXISTS memories_ad
                AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(
                        memories_fts, rowid, content, category
                    )
                    VALUES (
                        'delete', old.id, old.content, old.category
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS memories_au
                AFTER UPDATE ON memories BEGIN
                    INSERT INTO memories_fts(
                        memories_fts, rowid, content, category
                    )
                    VALUES (
                        'delete', old.id, old.content, old.category
                    );

                    INSERT INTO memories_fts(rowid, content, category)
                    VALUES (new.id, new.content, new.category);
                END;
            """)

            # Rebuild the index if the FTS table was newly created
            # or if a previous process stopped during initialization.
            connection.execute("""
                INSERT INTO memories_fts(memories_fts)
                VALUES ('rebuild')
            """)

    @staticmethod
    def _validate_category(category):
        if not isinstance(category, str) or category not in CATEGORIES:
            raise ValueError(
                "Category must be one of: "
                + ", ".join(sorted(CATEGORIES))
            )
        return category

    @staticmethod
    def _validate_content(content):
        if not isinstance(content, str):
            raise TypeError("Memory content must be text.")

        content = content.strip()

        if not content:
            raise ValueError("Memory content cannot be empty.")

        if len(content) > MAX_CONTENT_LENGTH:
            raise ValueError(
                f"Memory content cannot exceed {MAX_CONTENT_LENGTH} characters."
            )

        return content

    @staticmethod
    def _validate_id(memory_id):
        if (
            not isinstance(memory_id, int)
            or isinstance(memory_id, bool)
            or memory_id < 1
        ):
            raise ValueError("Memory ID must be a positive integer.")
        return memory_id

    def add(self, content, category):
        """Explicitly save one memory and return its ID."""
        content = self._validate_content(content)
        category = self._validate_category(category)
        timestamp = _timestamp()

        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO memories
                    (category, content, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (category, content, timestamp, timestamp),
            )
            return int(cursor.lastrowid)

    def get(self, memory_id):
        """Retrieve one memory by ID, or return None."""
        memory_id = self._validate_id(memory_id)

        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT id, category, content, created_at, updated_at
                FROM memories
                WHERE id = ?
                """,
                (memory_id,),
            ).fetchone()

        return dict(row) if row else None

    def list(self, category=None, limit=100):
        """List recent memories, optionally filtered by category."""
        if category is not None:
            category = self._validate_category(category)

        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 500
        ):
            raise ValueError("Limit must be between 1 and 500.")

        sql = """
            SELECT id, category, content, created_at, updated_at
            FROM memories
        """
        parameters = []

        if category is not None:
            sql += " WHERE category = ?"
            parameters.append(category)

        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        parameters.append(limit)

        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(sql, parameters).fetchall()

        return [dict(row) for row in rows]

    def search(self, query, category=None, limit=10):
        """Search memory text locally using SQLite FTS5."""
        if not isinstance(query, str):
            raise TypeError("Search query must be text.")

        query = query.strip()

        if not query:
            return []

        if len(query) > MAX_QUERY_LENGTH:
            raise ValueError(
                f"Search query cannot exceed {MAX_QUERY_LENGTH} characters."
            )

        if category is not None:
            category = self._validate_category(category)

        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 100
        ):
            raise ValueError("Limit must be between 1 and 100.")

        # Build a safe FTS expression from words, not raw user syntax.
        tokens = re.findall(r"[^\W_]+", query, flags=re.UNICODE)

        if not tokens:
            return []

        expression = " AND ".join(
            '"' + token.replace('"', '""') + '"*'
            for token in tokens
        )

        sql = """
            SELECT
                m.id,
                m.category,
                m.content,
                m.created_at,
                m.updated_at,
                bm25(memories_fts) AS relevance
            FROM memories_fts
            JOIN memories AS m ON m.id = memories_fts.rowid
            WHERE memories_fts MATCH ?
        """
        parameters = [expression]

        if category is not None:
            sql += " AND m.category = ?"
            parameters.append(category)

        sql += " ORDER BY relevance, m.updated_at DESC LIMIT ?"
        parameters.append(limit)

        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(sql, parameters).fetchall()

        return [
            {key: row[key] for key in row.keys() if key != "relevance"}
            for row in rows
        ]

    def update(self, memory_id, content, category=None):
        """Update an existing memory."""
        memory_id = self._validate_id(memory_id)
        content = self._validate_content(content)

        if category is not None:
            category = self._validate_category(category)

        timestamp = _timestamp()

        with self._lock, closing(self._connect()) as connection, connection:
            if category is None:
                cursor = connection.execute(
                    """
                    UPDATE memories
                    SET content = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (content, timestamp, memory_id),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE memories
                    SET content = ?, category = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (content, category, timestamp, memory_id),
                )

            return cursor.rowcount > 0

    def delete(self, memory_id):
        """Delete one memory by ID."""
        memory_id = self._validate_id(memory_id)

        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM memories WHERE id = ?",
                (memory_id,),
            )
            return cursor.rowcount > 0

    def count(self):
        """Return the number of saved memories."""
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM memories"
            ).fetchone()

        return int(row["total"])
