"""Isolated, local full-text document index for Misty, backed by SQLite FTS5.

This module is deliberately self-contained. It imports no Qt, model, speech,
or desktop-automation module, writes only to its own database file, and never
logs document content. Text extraction is delegated to
:class:`app.documents.reader.DocumentReader`, which enforces the same
canonical-root and symlink containment rules used by the rest of Misty.

Storage choice
--------------
The FTS table uses FTS5's **external-content** mode (``content='documents'``):
the index stores only the inverted index, while the authoritative text lives
in the ``documents`` table. Triggers keep the two in sync. External content was
chosen over a contentless table because :func:`snippet` and :func:`bm25` need
the indexed column values; a contentless FTS table cannot produce snippets
without re-reading every candidate document. It also avoids storing the text
twice. The update trigger is restricted to ``UPDATE OF title, content`` so
refreshing only freshness metadata never touches the FTS index.

Schema
------
``documents(id, root, rel_path, title, content, size, mtime, mtime_ns,
content_hash, indexed_at)`` with ``UNIQUE(root, rel_path)`` is the
authoritative store. ``documents_fts(title, content)`` is the external-content
FTS5 table over it. ``roots(root, indexed_at)`` records the last completed
scan time per scope. Search snippets use ``[[`` and ``]]`` as highlight
markers; only a bounded window around the match is ever returned.

Incremental strategy
--------------------
For every candidate file the scanner records size, ``st_mtime_ns``, and a
SHA-256 hash of the *extracted* text. A file whose size and mtime are both
unchanged is skipped without being read. A file whose mtime changed is read;
if the text hash is unchanged only its metadata is refreshed. Changed text is
re-indexed. Rows whose source file no longer exists are deleted, but stale-row
pruning only happens after a *complete* scan (no depth/file budget hit and no
directory-listing error), so a bounded or partially unreadable scan can never
delete rows it simply did not visit.

Scope and limits
----------------
``index_root`` accepts only directories inside ``allowed_base`` (the current
home directory by default, injected by tests). Roots are canonicalised, so a
symlink that resolves outside the base is rejected. Symlinked files and
directories inside a root are never followed.

Credential privacy
------------------
Paths that commonly hold credentials are skipped by default, both inside the
walked tree and when they are handed in as the scan root, so secrets are never
copied into the local index:

* hidden directories such as ``.ssh``, ``.gnupg``, ``.pki``,
  ``.password-store``, ``.aws``, ``.docker``, ``.kube``, ``.mozilla``,
  ``.thunderbird``, ``.config`` (unless one of its components is explicitly
  allowed), ``.local/share/keyrings`` and browser profile directories
  (``.config/google-chrome``, ``.config/chromium``, ``.mozilla/firefox``);
* credential files such as ``id_rsa``, ``id_ed25519``, ``*.pem``, ``*.key``,
  ``*.p12``, ``*.pfx``, ``*.kdbx``, ``*.gpg``, ``credentials*``, ``secrets*``,
  ``.env*``, ``.netrc``, ``cookies.sqlite`` and ``logins.json``.

The defaults are :data:`DEFAULT_EXCLUDED_PATTERNS`; ``excluded_patterns``
replaces or extends them per instance, and ``allowed_patterns`` re-admits a
specific match (for example ``"*.env"``) when indexing it is intentional.
Patterns are ``fnmatch`` globs matched case-insensitively against the entry
name, or against the relative path when they contain ``/``.

Scan budgets
------------
A scan visits at most ``max_entries`` entries, opens at most
``max_directories`` directories, keeps its stack at most ``max_queue`` deep,
and stops after ``max_scan_seconds``; pruning only runs after a complete,
untruncated scan. A new document is never read or hashed once the global
document capacity (``max_total_documents``) is full. Depth, files per scan,
per-file bytes, per-file characters, query length, query tokens, result limit,
snippet length, and excerpt length are bounded too.

Freshness
---------
``search(..., verify_sources=True)`` (the default) stats each hit's source
file and labels it ``missing`` when it was deleted, ``changed`` when its size
or ``mtime_ns`` no longer matches the index, and ``present`` otherwise.

Public API
----------
``KnowledgeIndex(database_path, *, allowed_base=None, ...)``
    Open (and create if needed) the index database.
``index_root(root)``
    Incrementally index one directory tree; returns a structured result dict.
``search(query, limit=10, verify_sources=True)``
    Return ranked hits with path, title, highlighted snippet, rank, mtime,
    a bounded plain-text excerpt, and a ``status``/``stale`` freshness flag.
``stats()``
    Return the indexed document count, roots that currently hold documents,
    and total indexed bytes.
``remove_root(root)`` / ``clear()``
    Delete rows from the local database only. Source files are never touched.

Test isolation
--------------
All tests construct a :class:`tempfile.TemporaryDirectory`, inject it as
``allowed_base``, and keep the database inside it. Nothing here reads or
writes the real user home unless a caller explicitly points a root there.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from app.actions.file_search import FileSearchService
from app.documents.reader import SUPPORTED_EXTENSIONS, DocumentReader

INDEXABLE_EXTENSIONS = SUPPORTED_EXTENSIONS | frozenset({
    ".pdf", ".docx", ".odt",
})


DEFAULT_MAX_FILE_BYTES = 1_000_000
DEFAULT_MAX_CONTENT_CHARS = 200_000
DEFAULT_MAX_TOTAL_DOCUMENTS = 10_000
DEFAULT_MAX_DEPTH = 8
DEFAULT_MAX_FILES_PER_ROOT = 5_000
DEFAULT_MAX_ENTRIES = 50_000
DEFAULT_MAX_DIRECTORIES = 5_000
DEFAULT_MAX_QUEUE = 10_000
DEFAULT_MAX_SCAN_SECONDS = 20.0

# Sentinel telling :meth:`KnowledgeIndex.is_private_path` to derive the path
# relative to ``allowed_base`` from the absolute path it was given.
_ABSOLUTE_PATH = object()
DEFAULT_MAX_QUERY_CHARS = 200
DEFAULT_MAX_QUERY_TOKENS = 16
DEFAULT_MAX_SNIPPET_CHARS = 240
DEFAULT_MAX_TEXT_CHARS = 400

MAX_RESULTS = 50
SNIPPET_START = "[["
SNIPPET_END = "]]"
SNIPPET_ELLIPSIS = "…"
SNIPPET_TOKENS = 32
TEXT_TOKENS = 64
TITLE_RANK_WEIGHT = 4.0

DEFAULT_EXCLUDED_PATTERNS = (
    # Hidden directories that hold credentials or browser profile data.
    ".ssh",
    ".gnupg",
    ".pki",
    ".password-store",
    ".local/share/keyrings",
    ".aws",
    ".docker",
    ".kube",
    ".mozilla",
    ".thunderbird",
    ".config",
    ".config/google-chrome",
    ".config/chromium",
    ".config/microsoft-edge",
    ".config/brave-browser",
    ".local/share/telegramdesktop",
    # Credential file names (case-insensitive; suffixes also match names
    # such as "id_rsa.pub" or "server.pem").
    "*id_rsa*",
    "*id_dsa*",
    "*id_ecdsa*",
    "*id_ed25519*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.kdbx",
    "*.gpg",
    "*.asc",
    "*.env",
    ".env",
    ".env.*",
    "credentials*",
    "secrets*",
    "*credentials.json",
    "*secrets.json",
    ".netrc",
    "_netrc",
    "cookies.sqlite",
    "logins.json",
    "key4.db",
    "login data",
    "*.ovpn",
)

_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS roots (
    root TEXT PRIMARY KEY,
    indexed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    mtime_ns INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    indexed_at REAL NOT NULL,
    UNIQUE(root, rel_path)
);

CREATE INDEX IF NOT EXISTS documents_root_idx ON documents(root);

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    title,
    content,
    content='documents',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS documents_ai
AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, title, content)
    VALUES (new.id, new.title, new.content);
END;

CREATE TRIGGER IF NOT EXISTS documents_ad
AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, content)
    VALUES ('delete', old.id, old.title, old.content);
END;

CREATE TRIGGER IF NOT EXISTS documents_au
AFTER UPDATE OF title, content ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, content)
    VALUES ('delete', old.id, old.title, old.content);

    INSERT INTO documents_fts(rowid, title, content)
    VALUES (new.id, new.title, new.content);
END;
"""

_SEARCH_SQL = f"""
SELECT
    d.id AS id,
    d.root AS root,
    d.rel_path AS rel_path,
    d.title AS title,
    d.size AS size,
    d.mtime AS mtime,
    d.mtime_ns AS mtime_ns,
    snippet(documents_fts, 1, ?, ?, ?, ?) AS snippet,
    snippet(documents_fts, 1, '', '', ?, ?) AS excerpt,
    bm25(documents_fts, {TITLE_RANK_WEIGHT}, 1.0) AS rank
FROM documents_fts
JOIN documents AS d ON d.id = documents_fts.rowid
WHERE documents_fts MATCH ?
ORDER BY rank ASC, d.root ASC, d.rel_path ASC
LIMIT ?
"""

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")

_ROOT_MESSAGES = {
    "invalid_root": "An approved folder path is required.",
    "root_not_found": "The requested folder does not exist.",
    "root_unavailable": "The requested folder is unavailable.",
    "root_not_directory": "The requested folder is not a directory.",
    "root_outside_scope": (
        "The requested folder is outside the allowed document area."
    ),
    "root_private": (
        "That folder looks like credential or browser-profile storage, so it "
        "is never indexed."
    ),
}


class KnowledgeIndexError(ValueError):
    """Raised only for invalid construction-time configuration."""


@dataclass(slots=True)
class _ScanResult:
    """Bounded scan output: candidate files plus skip counters."""

    files: list[tuple[str, os.stat_result]] = field(default_factory=list)
    skipped_unsupported: int = 0
    skipped_excluded: int = 0
    skipped_symlinks: int = 0
    listing_errors: int = 0
    skipped_private: int = 0
    visited_entries: int = 0
    directories: int = 0
    truncated: bool = False
    truncation_reason: str | None = None


@dataclass(slots=True)
class _IndexState:
    """Mutable per-scan counters and the global document total."""

    total: int
    indexed: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    skipped_unsupported: int = 0
    skipped_oversized: int = 0
    skipped_document_budget: int = 0
    errors: int = 0


class KnowledgeIndex:
    """Local SQLite FTS5 index of supported text documents.

    The object holds configuration only; every operation opens its own short
    SQLite connection, so an instance may be shared by several threads.
    Construction raises :class:`KnowledgeIndexError` for invalid settings.
    Runtime failures are reported as structured ``{"success": False, ...}``
    dictionaries and never raise with a path or document fragment attached.
    """

    def __init__(
        self,
        database_path,
        *,
        allowed_base=None,
        max_file_bytes=DEFAULT_MAX_FILE_BYTES,
        max_content_chars=DEFAULT_MAX_CONTENT_CHARS,
        max_total_documents=DEFAULT_MAX_TOTAL_DOCUMENTS,
        max_depth=DEFAULT_MAX_DEPTH,
        max_files_per_root=DEFAULT_MAX_FILES_PER_ROOT,
        max_query_chars=DEFAULT_MAX_QUERY_CHARS,
        max_query_tokens=DEFAULT_MAX_QUERY_TOKENS,
        max_snippet_chars=DEFAULT_MAX_SNIPPET_CHARS,
        max_text_chars=DEFAULT_MAX_TEXT_CHARS,
        max_entries=DEFAULT_MAX_ENTRIES,
        max_directories=DEFAULT_MAX_DIRECTORIES,
        max_queue=DEFAULT_MAX_QUEUE,
        max_scan_seconds=DEFAULT_MAX_SCAN_SECONDS,
        excluded_patterns=None,
        allowed_patterns=(),
    ):
        self._max_file_bytes = self._setting(max_file_bytes, "max_file_bytes")
        self._max_content_chars = self._setting(
            max_content_chars, "max_content_chars"
        )
        self._max_total_documents = self._setting(
            max_total_documents, "max_total_documents"
        )
        self._max_depth = self._setting(max_depth, "max_depth", minimum=0)
        self._max_files_per_root = self._setting(
            max_files_per_root, "max_files_per_root"
        )
        self._max_query_chars = self._setting(
            max_query_chars, "max_query_chars"
        )
        self._max_query_tokens = self._setting(
            max_query_tokens, "max_query_tokens"
        )
        self._max_snippet_chars = self._setting(
            max_snippet_chars, "max_snippet_chars"
        )
        self._max_text_chars = self._setting(
            max_text_chars, "max_text_chars"
        )
        self._max_entries = self._setting(max_entries, "max_entries")
        self._max_directories = self._setting(
            max_directories, "max_directories"
        )
        self._max_queue = self._setting(max_queue, "max_queue")
        if (
            isinstance(max_scan_seconds, bool)
            or not isinstance(max_scan_seconds, (int, float))
            or not 0 < float(max_scan_seconds) <= 600
        ):
            raise KnowledgeIndexError(
                "max_scan_seconds must be a number of seconds in (0, 600]."
            )
        self._max_scan_seconds = float(max_scan_seconds)
        self._excluded_patterns = self._patterns(
            DEFAULT_EXCLUDED_PATTERNS if excluded_patterns is None
            else excluded_patterns,
            "excluded_patterns",
        )
        self._allowed_patterns = self._patterns(
            allowed_patterns, "allowed_patterns"
        )

        self._allowed_base = self._resolve_base(allowed_base)
        self._database = self._database_path(database_path)
        try:
            self._initialize()
        except (sqlite3.Error, OSError):
            raise KnowledgeIndexError(
                "The knowledge index database could not be initialized."
            ) from None

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @property
    def allowed_base(self) -> Path:
        """Canonical directory that every indexed root must stay inside."""
        return self._allowed_base

    @property
    def database_path(self) -> Path:
        """Canonical path of the local SQLite database."""
        return self._database

    @staticmethod
    def _setting(value, name, minimum=1):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < minimum
        ):
            raise KnowledgeIndexError(
                f"{name} must be an integer of at least {minimum}."
            )
        return value

    @staticmethod
    def _patterns(value, name):
        if isinstance(value, str) or value is None:
            raise KnowledgeIndexError(f"{name} must be a sequence of globs.")
        try:
            patterns = tuple(value)
        except TypeError:
            raise KnowledgeIndexError(
                f"{name} must be a sequence of globs."
            ) from None
        normalized = []
        for pattern in patterns:
            if (
                not isinstance(pattern, str)
                or not pattern.strip()
                or len(pattern) > 200
                or "\x00" in pattern
            ):
                raise KnowledgeIndexError(
                    f"{name} entries must be short non-empty strings."
                )
            normalized.append(pattern.strip().casefold())
        return tuple(normalized)

    @property
    def excluded_patterns(self) -> tuple[str, ...]:
        """Case-folded exclusion patterns active for this index."""
        return self._excluded_patterns

    @property
    def allowed_patterns(self) -> tuple[str, ...]:
        """Case-folded patterns that re-admit excluded paths."""
        return self._allowed_patterns

    @staticmethod
    def _coerce_path(value):
        if not isinstance(value, (str, os.PathLike)):
            return None
        try:
            raw = os.fspath(value)
        except TypeError:
            return None
        if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
            return None
        return raw

    @classmethod
    def _resolve_base(cls, allowed_base) -> Path:
        if allowed_base is None:
            raw = Path.home()
        else:
            raw = cls._coerce_path(allowed_base)
            if raw is None:
                raise KnowledgeIndexError(
                    "An allowed base directory is required."
                )
        try:
            base = Path(raw).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise KnowledgeIndexError(
                "The allowed base directory is unavailable."
            ) from None
        if not base.is_dir():
            raise KnowledgeIndexError(
                "The allowed base directory is not a directory."
            )
        return base

    @classmethod
    def _database_path(cls, database_path) -> Path:
        raw = cls._coerce_path(database_path)
        if raw is None:
            raise KnowledgeIndexError("A database path is required.")
        database = Path(raw).expanduser()
        try:
            database.parent.mkdir(parents=True, exist_ok=True)
            database = database.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            raise KnowledgeIndexError(
                "The knowledge index database path is unavailable."
            ) from None
        if database.is_dir():
            raise KnowledgeIndexError(
                "The knowledge index database path is a directory."
            )
        return database

    def _connect(self):
        connection = sqlite3.connect(self._database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.isolation_level = None
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self):
        created = not self._database.exists()
        with closing(self._connect()) as connection:
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = NORMAL")
            except sqlite3.Error:
                pass
            had_fts = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'documents_fts'"
            ).fetchone() is not None
            connection.executescript(_SCHEMA)
            if not had_fts:
                # Recover an index whose FTS table was missing but whose
                # documents rows survived an interrupted setup.
                connection.execute(
                    "INSERT INTO documents_fts(documents_fts) "
                    "VALUES ('rebuild')"
                )
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        if created:
            self._restrict_database_permissions()

    def _restrict_database_permissions(self):
        try:
            os.chmod(self._database, 0o600)
        except OSError:
            pass

    def close(self):
        """Release resources.

        Connections are opened per operation, so there is nothing persistent
        to close. The method exists so callers can use the index as a managed
        resource without special-casing it.
        """
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def index_root(self, root):
        """Incrementally index one folder inside :attr:`allowed_base`.

        Returns a structured dictionary with ``success``, skip counters, and
        ``truncated``/``truncation_reason`` flags. Only files whose extension
        is in :data:`INDEXABLE_EXTENSIONS` (text, HTML, PDF, DOCX, ODT) is read, and
        only through :class:`DocumentReader`.
        """
        try:
            resolved, error = self._resolve_root(root)
            if error is not None:
                return self._index_failure(error, _ROOT_MESSAGES[error])
            return self._index_canonical_root(resolved)
        except sqlite3.Error:
            return self._index_failure(
                "storage_error",
                "The knowledge index database is unavailable.",
            )
        except (OSError, RuntimeError):
            return self._index_failure(
                "index_error",
                "The document index could not scan the requested folder.",
            )
        except Exception:
            # Deliberately generic: no exception text or content may escape.
            return self._index_failure(
                "index_error",
                "The document index could not complete the scan.",
            )

    def _resolve_root(self, root):
        """Return ``(canonical_path, error_code)`` for a candidate root."""
        raw = self._coerce_path(root)
        if raw is None:
            return None, "invalid_root"
        try:
            resolved = Path(raw).expanduser().resolve(strict=True)
        except FileNotFoundError:
            return None, "root_not_found"
        except (OSError, RuntimeError, ValueError):
            return None, "root_unavailable"
        if not resolved.is_dir():
            return None, "root_not_directory"
        if resolved != self._allowed_base and not resolved.is_relative_to(
            self._allowed_base
        ):
            return None, "root_outside_scope"
        if self.is_private_path(resolved):
            return None, "root_private"
        return resolved, None

    def _base_relative(self, path):
        """Path relative to :data:`allowed_base` as a POSIX string."""
        try:
            resolved = Path(path)
            return str(resolved.relative_to(self._allowed_base))
        except (OSError, ValueError):
            return str(path)

    def excluded_pattern_for(self, name, base_relative=None):
        """Return the exclusion glob matching ``name``, or ``None``.

        ``name`` is matched case-insensitively against every glob; a glob may
        also contain ``/`` and then matches the entry's path relative to
        :attr:`allowed_base`. A matching ``allowed_patterns`` entry re-admits
        this exact path, whatever excluded it.
        """
        folded_name = str(name).casefold()
        relative = (base_relative or "").strip("/").casefold()

        matched = [
            pattern for pattern in self._excluded_patterns
            if self._pattern_hits(pattern, folded_name, relative)
        ]
        if not matched:
            return None
        for glob in self._allowed_patterns:
            if glob in matched or self._pattern_hits(
                glob, folded_name, relative
            ):
                return None
        return matched[0]

    @staticmethod
    def _pattern_hits(pattern, folded_name, relative):
        if fnmatch.fnmatchcase(folded_name, pattern):
            return True
        if "/" not in pattern or not relative:
            return False
        return bool(
            fnmatch.fnmatchcase(relative, pattern)
            or fnmatch.fnmatchcase(relative, "*" + pattern)
            or relative == pattern
            or relative.startswith(pattern + "/")
        )

    def is_private_path(self, path, **hint):
        """Whether ``path`` is a credential path excluded by default.

        ``hint`` may carry an already-known ``name`` and the path's
        ``relative`` form under :attr:`allowed_base` so a scan never resolves
        a path twice.
        """
        name = hint.get("name") or os.path.basename(str(path))
        relative = hint.get("relative", "")
        base_relative = (
            self._base_relative(path) if relative is _ABSOLUTE_PATH
            else relative
        )
        if self.excluded_pattern_for(name, base_relative) is not None:
            return True
        # A file inside an excluded location is private even when the file
        # name itself is innocuous (for example ``.ssh/config``).
        parts = str(base_relative).replace("\\", "/").split("/")
        return any(
            self.excluded_pattern_for(part) is not None
            for part in parts[:-1]
        )

    def _index_canonical_root(self, root):
        root_text = str(root)
        try:
            reader = DocumentReader(
                root,
                max_bytes=self._max_file_bytes,
                max_chars=self._max_content_chars,
            )
        except (OSError, RuntimeError, ValueError):
            return self._index_failure(
                "root_unavailable",
                "The requested folder is unavailable.",
            )

        scan = self._scan(root)
        present = {relative for relative, _ in scan.files}
        state = _IndexState(total=0)
        # Pruning is only sound for a complete scan. A listing error, any
        # scan budget stop, or a credential-excluded entry all make the walk
        # partial, so a row the scan simply did not visit is never deleted.
        prune_eligible = not scan.truncated and scan.listing_errors == 0

        with closing(self._connect()) as connection:
            existing = self._existing_documents(connection, root_text)
            state.total = self._document_count(connection)
            if prune_eligible and self._has_excluded_rows(
                connection,
                root_text,
                seen=frozenset(
                    relative for relative, _ in scan.files
                ),
            ):
                # A row that still exists but is now credential-excluded
                # must survive; prune_forgetfully removes it explicitly.
                prune_eligible = False

            for relative, stat_result in scan.files:
                present_for_prune = self._index_one(
                    connection,
                    reader,
                    root_text,
                    relative,
                    stat_result,
                    existing,
                    state,
                )
                if not present_for_prune:
                    present.discard(relative)

            if prune_eligible:
                self._prune_missing(connection, root_text, present, state)

            self._write(
                connection,
                "INSERT INTO roots (root, indexed_at) VALUES (?, ?) "
                "ON CONFLICT(root) DO UPDATE SET "
                "indexed_at = excluded.indexed_at",
                (root_text, time.time()),
            )
            indexed_total = self._document_count(connection)

        return {
            "success": True,
            "root": root_text,
            "scanned": len(scan.files),
            "indexed": state.indexed,
            "updated": state.updated,
            "unchanged": state.unchanged,
            "deleted": state.deleted,
            "skipped_unsupported": (
                scan.skipped_unsupported + state.skipped_unsupported
            ),
            "skipped_oversized": state.skipped_oversized,
            "skipped_excluded": scan.skipped_excluded,
            "skipped_symlinks": scan.skipped_symlinks,
            "skipped_private": scan.skipped_private,
            "skipped_document_budget": state.skipped_document_budget,
            "visited_entries": scan.visited_entries,
            "directories": scan.directories,
            "errors": scan.listing_errors + state.errors,
            "indexed_total": indexed_total,
            "truncated": scan.truncated,
            "truncation_reason": scan.truncation_reason,
            "document_budget_reached": state.skipped_document_budget > 0,
            "message": (
                f"Indexed {state.indexed} new file(s); updated "
                f"{state.updated}, unchanged {state.unchanged}, removed "
                f"{state.deleted}."
            ),
        }

    def _scan(self, root) -> _ScanResult:
        """Walk ``root`` within every budget, never following symlinks.

        A budget stop sets ``truncated`` with a reason and ends the walk, so
        an enormous or adversarial tree costs at most ``max_entries``
        ``scandir`` entries even when almost none of them is indexable.
        """
        result = _ScanResult()
        stack = [("", root, 0)]
        deadline = time.monotonic() + self._max_scan_seconds

        while stack:
            if result.truncated:
                break
            if time.monotonic() > deadline:
                result.truncated = True
                result.truncation_reason = "time budget"
                break
            relative_dir, directory, depth = stack.pop()
            child_directories = []
            try:
                with os.scandir(directory) as iterator:
                    result.directories += 1
                    if result.directories > self._max_directories:
                        result.truncated = True
                        result.truncation_reason = "directory budget"
                        break
                    for entry in iterator:
                        if len(result.files) >= self._max_files_per_root:
                            result.truncated = True
                            result.truncation_reason = "file budget"
                            break
                        if result.visited_entries >= self._max_entries:
                            result.truncated = True
                            result.truncation_reason = "entry budget"
                            break
                        result.visited_entries += 1

                        name = entry.name
                        if not self._storable(name):
                            result.listing_errors += 1
                            continue
                        relative = (
                            f"{relative_dir}/{name}"
                            if relative_dir else name
                        )
                        try:
                            if entry.is_symlink():
                                result.skipped_symlinks += 1
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if self._excluded_directory(
                                    relative, name
                                ):
                                    result.skipped_excluded += 1
                                    continue
                                if self.is_private_path(
                                    entry.path,
                                    name=name,
                                    relative=_ABSOLUTE_PATH,
                                ):
                                    result.skipped_private += 1
                                    continue
                                if depth + 1 > self._max_depth:
                                    result.truncated = True
                                    result.truncation_reason = (
                                        "depth budget"
                                    )
                                    continue
                                if (
                                    len(stack) + len(child_directories)
                                    >= self._max_queue
                                ):
                                    result.truncated = True
                                    result.truncation_reason = (
                                        "queue bound"
                                    )
                                    break
                                child_directories.append(
                                    (relative, entry.path, depth + 1)
                                )
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            # Privacy first: credential names are reported as
                            # private even when their extension is unsupported.
                            if self.is_private_path(
                                entry.path,
                                name=name,
                                relative=_ABSOLUTE_PATH,
                            ):
                                result.skipped_private += 1
                                continue
                            if (
                                os.path.splitext(name)[1].lower()
                                not in INDEXABLE_EXTENSIONS
                            ):
                                result.skipped_unsupported += 1
                                continue
                            stat_result = entry.stat(follow_symlinks=False)
                        except OSError:
                            result.listing_errors += 1
                            continue

                        result.files.append((relative, stat_result))
            except OSError:
                result.listing_errors += 1
                continue

            stack.extend(child_directories)

        return result

    @staticmethod
    def _storable(value) -> bool:
        """Whether a filename can be stored as UTF-8 in SQLite TEXT."""
        try:
            value.encode("utf-8", "strict")
        except (UnicodeEncodeError, AttributeError):
            return False
        return True

    @staticmethod
    def _excluded_directory(relative, name) -> bool:
        """Reuse FileSearchService's exclusion list for the document tree."""
        if name.casefold() in FileSearchService.EXCLUDED_NAMES:
            return True
        normalized = relative.replace(os.sep, "/").casefold()
        return normalized == ".local/share/trash" or normalized.startswith(
            ".local/share/trash/"
        )

    def _index_one(
        self,
        connection,
        reader,
        root,
        relative,
        stat_result,
        existing,
        state,
    ) -> bool:
        """Index one candidate file; return whether it stays prune-visible."""
        title = relative.rsplit("/", 1)[-1]
        row = existing.get(relative)
        now = time.time()

        if (
            row is not None
            and int(row["size"]) == stat_result.st_size
            and int(row["mtime_ns"]) == stat_result.st_mtime_ns
        ):
            state.unchanged += 1
            return True

        if stat_result.st_size > self._max_file_bytes:
            state.skipped_oversized += 1
            self._delete_document(connection, row, state)
            return False

        if row is None and state.total >= self._max_total_documents:
            # Capacity is checked before the read and hash on purpose: a full
            # index never spends work extracting new documents.
            state.skipped_document_budget += 1
            return False

        outcome = reader.read(relative)
        if not outcome.ok:
            code = outcome.error or "read_error"
            if code == "file_too_large":
                state.skipped_oversized += 1
            elif code == "unsupported_format":
                state.skipped_unsupported += 1
            else:
                # Includes files that vanished or became symlinks mid-scan.
                state.errors += 1
            self._delete_document(connection, row, state)
            return False

        text = outcome.text
        if len(text) > self._max_content_chars:
            text = text[: self._max_content_chars]
        digest = hashlib.sha256(
            text.encode("utf-8", "replace")
        ).hexdigest()

        if row is not None and row["content_hash"] == digest:
            # Same extracted text: refresh freshness metadata only. The
            # FTS update trigger excludes these columns on purpose.
            self._write(
                connection,
                "UPDATE documents SET size = ?, mtime = ?, mtime_ns = ?, "
                "indexed_at = ? WHERE id = ?",
                (
                    stat_result.st_size,
                    stat_result.st_mtime,
                    stat_result.st_mtime_ns,
                    now,
                    row["id"],
                ),
            )
            state.unchanged += 1
            return True

        if row is None:
            self._write(
                connection,
                "INSERT INTO documents "
                "(root, rel_path, title, content, size, mtime, mtime_ns, "
                "content_hash, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    root,
                    relative,
                    title,
                    text,
                    stat_result.st_size,
                    stat_result.st_mtime,
                    stat_result.st_mtime_ns,
                    digest,
                    now,
                ),
            )
            state.indexed += 1
            state.total += 1
        else:
            self._write(
                connection,
                "UPDATE documents SET title = ?, content = ?, size = ?, "
                "mtime = ?, mtime_ns = ?, content_hash = ?, indexed_at = ? "
                "WHERE id = ?",
                (
                    title,
                    text,
                    stat_result.st_size,
                    stat_result.st_mtime,
                    stat_result.st_mtime_ns,
                    digest,
                    now,
                    row["id"],
                ),
            )
            state.updated += 1
        return True

    def _delete_document(self, connection, row, state):
        """Delete one stale row (and its FTS entry through the trigger)."""
        if row is None:
            return
        self._write(
            connection,
            "DELETE FROM documents WHERE id = ?",
            (row["id"],),
        )
        state.deleted += 1

    def _has_excluded_rows(self, connection, root, seen=()):
        """Whether ``root`` holds a still-present row that is now excluded.

        Rows already deleted during this scan (vanished or unreadable files)
        are ignored; only rows whose file exists but is excluded block
        pruning, because otherwise a later non-excluding scan would silently
        re-index private content.
        """
        rows = connection.execute(
            "SELECT rel_path FROM documents WHERE root = ?",
            (root,),
        ).fetchall()
        base_relative = self._base_relative(root)
        for row in rows:
            if seen and row["rel_path"] not in seen:
                continue
            relative = row["rel_path"]
            folded = f"{base_relative}/{relative}".strip("/").casefold()
            if any(
                self.excluded_pattern_for(part) is not None
                for part in folded.split("/")
                if part
            ):
                return True
        return False

    def _prune_missing(self, connection, root, present, state):
        """Delete rows for files not seen during a complete scan of ``root``."""
        rows = connection.execute(
            "SELECT rel_path FROM documents WHERE root = ?",
            (root,),
        ).fetchall()
        stale = sorted(
            {row["rel_path"] for row in rows} - present
        )
        if not stale:
            return

        connection.execute("BEGIN IMMEDIATE")
        try:
            for relative in stale:
                connection.execute(
                    "DELETE FROM documents WHERE root = ? AND rel_path = ?",
                    (root, relative),
                )
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        else:
            connection.execute("COMMIT")
        state.deleted += len(stale)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query, limit=10, verify_sources=True):
        """Search indexed text with a safely tokenized FTS5 query.

        ``limit`` must be an integer from 1 to :data:`MAX_RESULTS`. The query
        is reduced to Unicode word tokens and quoted, so FTS5 operators or
        SQL-looking input cannot alter the statement. Each hit carries its
        absolute path, title, snippet with ``[[``/``]]`` markers, rank (lower
        is better, mtime, and a bounded plain excerpt. ``truncated`` is true
        only when more matches existed than the requested limit.

        With ``verify_sources=True`` (the default) every hit is checked with
        one ``lstat`` of its source file: ``status`` is ``"present"``,
        ``"changed"`` (size or ``mtime_ns`` differs from the indexed values)
        or ``"missing"`` (the file is gone), and ``stale`` is true for the
        latter two. Stale rows stay searchable until a complete re-index
        prunes them; callers should treat ``missing`` results as outdated.
        """
        if not isinstance(query, str):
            return self._search_failure(
                "invalid_query", "Search text must be a string."
            )
        query = query.strip()
        if not query:
            return self._search_failure(
                "invalid_query", "Search text must not be empty."
            )
        if len(query) > self._max_query_chars:
            return self._search_failure(
                "query_too_long",
                "Search text exceeds the maximum query length.",
            )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_RESULTS
        ):
            return self._search_failure(
                "invalid_limit",
                f"Result limit must be between 1 and {MAX_RESULTS}.",
            )

        tokens = _TOKEN_RE.findall(query)[: self._max_query_tokens]
        if not tokens:
            return {
                "success": True,
                "query": query,
                "results": [],
                "count": 0,
                "limit": limit,
                "truncated": False,
                "message": "The query contains no searchable words.",
            }

        expression = " AND ".join(
            '"' + token.replace('"', '""') + '"*' for token in tokens
        )

        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    _SEARCH_SQL,
                    (
                        SNIPPET_START,
                        SNIPPET_END,
                        SNIPPET_ELLIPSIS,
                        SNIPPET_TOKENS,
                        SNIPPET_ELLIPSIS,
                        TEXT_TOKENS,
                        expression,
                        limit + 1,
                    ),
                ).fetchall()
        except sqlite3.Error:
            return self._search_failure(
                "storage_error",
                "The knowledge index database is unavailable.",
            )
        except Exception:
            return self._search_failure(
                "search_error",
                "The document search could not complete.",
            )

        has_more = len(rows) > limit
        results = []
        for row in rows[:limit]:
            result = self._result_from_row(row)
            if verify_sources:
                result["status"] = self._source_status(row, result["path"])
                result["stale"] = result["status"] != "present"
            results.append(result)
        return {
            "success": True,
            "query": query,
            "results": results,
            "count": len(results),
            "limit": limit,
            "truncated": has_more,
            "message": f"Found {len(results)} matching document(s).",
        }

    def _result_from_row(self, row):
        title = row["title"]
        root = row["root"]
        relative = row["rel_path"]
        return {
            "path": str(Path(root) / relative),
            "title": title,
            "snippet": self._excerpt(row["snippet"], self._max_snippet_chars),
            "text": self._excerpt(row["excerpt"], self._max_text_chars),
            "rank": round(float(row["rank"]), 6),
            "mtime": float(row["mtime"]),
            "size": int(row["size"]),
            "root": root,
            "relative_path": relative,
        }

    def _source_status(self, row, path):
        """Cheap freshness label for one indexed source (single ``lstat``)."""
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "unreadable"
        if (
            self._stat_size(info) == int(row["size"])
            and self._stat_mtime_ns(info) == int(row["mtime_ns"])
        ):
            return "present"
        return "changed"

    @staticmethod
    def _stat_size(info):
        return int(getattr(info, "st_size", -1))

    @staticmethod
    def _stat_mtime_ns(info):
        return int(getattr(info, "st_mtime_ns", -1))

    @classmethod
    def _excerpt(cls, value, limit) -> str:
        """Collapse whitespace, clip to ``limit`` chars, balance markers."""
        if not isinstance(value, str):
            return ""
        text = _WHITESPACE_RE.sub(" ", value).strip()
        if len(text) > limit:
            text = text[: max(0, limit - 1)]
            space = text.rfind(" ")
            if space >= limit // 2:
                text = text[:space]
            text = text.rstrip() + SNIPPET_ELLIPSIS
        return cls._balance_markers(text)

    @staticmethod
    def _balance_markers(text):
        """Remove highlight markers left dangling by snippet clipping."""
        while text.count(SNIPPET_START) > text.count(SNIPPET_END):
            index = text.rfind(SNIPPET_START)
            if index < 0:
                break
            text = text[:index] + text[index + len(SNIPPET_START):]
        while text.count(SNIPPET_END) > text.count(SNIPPET_START):
            index = text.rfind(SNIPPET_END)
            if index < 0:
                break
            text = text[:index] + text[index + len(SNIPPET_END):]
        return text

    # ------------------------------------------------------------------
    # Statistics and deletion
    # ------------------------------------------------------------------

    def stats(self):
        """Return indexed document count, roots, and total indexed bytes."""
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT root, COUNT(*) AS documents, "
                    "COALESCE(SUM(size), 0) AS bytes "
                    "FROM documents GROUP BY root ORDER BY root"
                ).fetchall()
        except sqlite3.Error:
            return self._stats_failure(
                "The knowledge index database is unavailable."
            )
        except Exception:
            return self._stats_failure(
                "The document index statistics are unavailable."
            )

        detail = [
            {
                "root": row["root"],
                "documents": int(row["documents"]),
                "bytes": int(row["bytes"]),
            }
            for row in rows
        ]
        return {
            "success": True,
            "indexed": sum(item["documents"] for item in detail),
            "documents": sum(item["documents"] for item in detail),
            "count": sum(item["documents"] for item in detail),
            "roots": [item["root"] for item in detail],
            "root_count": len(detail),
            "roots_detail": detail,
            "indexed_bytes": sum(item["bytes"] for item in detail),
            "message": (
                f"{sum(item['documents'] for item in detail)} document(s) "
                f"in {len(detail)} root(s)."
            ),
        }

    def status(self):
        """Report roots, document counts, and the last completed index time."""
        result = self.stats()
        result["retention"] = {
            "operation": "local_only",
            "deletes_source_files": False,
        }
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT root, indexed_at FROM roots ORDER BY root"
                ).fetchall()
                counts = {
                    row["root"]: int(row["documents"])
                    for row in connection.execute(
                        "SELECT root, COUNT(*) AS documents "
                        "FROM documents GROUP BY root"
                    ).fetchall()
                }
        except sqlite3.Error:
            result["last_indexed"] = []
            return result
        detail = {
            item["root"]: item for item in result.get("roots_detail", [])
        }
        last_indexed = []
        for row in rows:
            root = row["root"]
            entry = {
                "root": root,
                "indexed_at": float(row["indexed_at"]),
                "documents": counts.get(root, 0),
            }
            if root in detail:
                entry["bytes"] = detail[root]["bytes"]
            last_indexed.append(entry)
        result["last_indexed"] = last_indexed
        result["indexable"] = True
        result["limits"] = {
            "max_total_documents": self._max_total_documents,
            "max_files_per_root": self._max_files_per_root,
            "max_entries": self._max_entries,
            "max_directories": self._max_directories,
            "max_queue": self._max_queue,
            "max_scan_seconds": self._max_scan_seconds,
            "max_file_bytes": self._max_file_bytes,
            "max_depth": self._max_depth,
        }
        result["excluded_patterns"] = list(self._excluded_patterns)
        result["allowed_patterns"] = list(self._allowed_patterns)
        return result

    def remove_root(self, root):
        """Delete all rows for one root from the local database.

        Ground truth on disk is untouched. The path does not need to still
        exist; it is canonicalised lexically when possible so a removed
        directory can still be forgotten. Returns the number of documents
        deleted.
        """
        raw = self._coerce_path(root)
        if raw is None:
            return self._delete_failure(
                "invalid_root", "An approved folder path is required."
            )
        try:
            canonical = Path(raw).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return self._delete_failure(
                "root_unavailable", "The requested folder is unavailable."
            )

        try:
            with closing(self._connect()) as connection:
                cursor = self._delete_transaction(
                    connection,
                    "DELETE FROM documents WHERE root = ?",
                    (str(canonical),),
                )
                self._write(
                    connection,
                    "DELETE FROM roots WHERE root = ?",
                    (str(canonical),),
                )
        except sqlite3.Error:
            return self._delete_failure(
                "storage_error",
                "The knowledge index database is unavailable.",
            )
        except Exception:
            return self._delete_failure(
                "delete_error",
                "The indexed folder could not be removed.",
            )

        deleted = max(int(cursor.rowcount), 0)
        return {
            "success": True,
            "root": str(canonical),
            "deleted": deleted,
            "message": f"Removed {deleted} document(s) from the index.",
        }

    def clear(self):
        """Delete every indexed row from the local database.

        Source files are never modified. The FTS index is optimized after the
        delete so the database does not retain a large tombstone list.
        """
        try:
            with closing(self._connect()) as connection:
                cursor = self._delete_transaction(
                    connection, "DELETE FROM documents", ()
                )
                self._write(connection, "DELETE FROM roots", ())
                try:
                    connection.execute(
                        "INSERT INTO documents_fts(documents_fts) "
                        "VALUES ('optimize')"
                    )
                except sqlite3.Error:
                    pass
        except sqlite3.Error:
            return self._delete_failure(
                "storage_error",
                "The knowledge index database is unavailable.",
            )
        except Exception:
            return self._delete_failure(
                "delete_error", "The document index could not be cleared."
            )

        deleted = max(int(cursor.rowcount), 0)
        return {
            "success": True,
            "deleted": deleted,
            "message": f"Cleared {deleted} document(s) from the index.",
        }

    # ------------------------------------------------------------------
    # Small SQLite helpers
    # ------------------------------------------------------------------

    def _write(self, connection, sql, parameters):
        """Run one parameterized write inside its own short transaction."""
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(sql, parameters)
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        else:
            connection.execute("COMMIT")

    def _delete_transaction(self, connection, sql, parameters):
        """Run one DELETE and return its cursor, committed before return."""
        connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = connection.execute(sql, parameters)
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        else:
            connection.execute("COMMIT")
        return cursor

    @staticmethod
    def _existing_documents(connection, root):
        rows = connection.execute(
            "SELECT id, rel_path, size, mtime_ns, content_hash "
            "FROM documents WHERE root = ?",
            (root,),
        ).fetchall()
        return {row["rel_path"]: row for row in rows}

    @staticmethod
    def _document_count(connection):
        row = connection.execute(
            "SELECT COUNT(*) AS total FROM documents"
        ).fetchone()
        return int(row["total"])

    # ------------------------------------------------------------------
    # Structured failures
    # ------------------------------------------------------------------

    @staticmethod
    def _index_failure(error, message):
        return {
            "success": False,
            "error": error,
            "message": message,
            "root": None,
            "scanned": 0,
            "indexed": 0,
            "updated": 0,
            "unchanged": 0,
            "deleted": 0,
            "skipped_unsupported": 0,
            "skipped_oversized": 0,
            "skipped_excluded": 0,
            "skipped_symlinks": 0,
            "skipped_document_budget": 0,
            "errors": 0,
            "indexed_total": 0,
            "truncated": False,
            "truncation_reason": None,
            "document_budget_reached": False,
        }

    @staticmethod
    def _search_failure(error, message):
        return {
            "success": False,
            "error": error,
            "message": message,
            "query": None,
            "results": [],
            "count": 0,
            "limit": None,
            "truncated": False,
        }

    @staticmethod
    def _stats_failure(message):
        return {
            "success": False,
            "error": "storage_error",
            "message": message,
            "indexed": 0,
            "documents": 0,
            "count": 0,
            "roots": [],
            "root_count": 0,
            "roots_detail": [],
            "indexed_bytes": 0,
        }

    @staticmethod
    def _delete_failure(error, message):
        return {
            "success": False,
            "error": error,
            "message": message,
            "deleted": 0,
        }
