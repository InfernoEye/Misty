"""Isolated local document index/search for Misty.

The public entry point is :class:`KnowledgeIndex`, a self-contained SQLite
FTS5 index of supported text documents (``.txt``, ``.md``, ``.html``,
``.htm``). The package has no Qt, model, network, or database-driver
dependencies beyond the standard library's ``sqlite3``, so it can be wired
into a tool registry without starting a UI or loading a model.
"""

from app.documents.reader import SUPPORTED_EXTENSIONS
from app.knowledge.index import (
    DEFAULT_MAX_CONTENT_CHARS,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_FILES_PER_ROOT,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_QUERY_CHARS,
    DEFAULT_MAX_QUERY_TOKENS,
    DEFAULT_MAX_SNIPPET_CHARS,
    DEFAULT_MAX_TEXT_CHARS,
    DEFAULT_MAX_TOTAL_DOCUMENTS,
    MAX_RESULTS,
    SNIPPET_END,
    SNIPPET_START,
    KnowledgeIndex,
    KnowledgeIndexError,
)

__all__ = [
    "DEFAULT_MAX_CONTENT_CHARS",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_FILES_PER_ROOT",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_QUERY_CHARS",
    "DEFAULT_MAX_QUERY_TOKENS",
    "DEFAULT_MAX_SNIPPET_CHARS",
    "DEFAULT_MAX_TEXT_CHARS",
    "DEFAULT_MAX_TOTAL_DOCUMENTS",
    "MAX_RESULTS",
    "SNIPPET_END",
    "SNIPPET_START",
    "SUPPORTED_EXTENSIONS",
    "KnowledgeIndex",
    "KnowledgeIndexError",
]
