"""Read-only, bounded document extraction for Misty.

The public entry point is :class:`DocumentReader`. The package is deliberately
free of Qt, model, database, and desktop-automation imports, so it can be
wired into a tool registry without starting any user interface.
"""

from app.documents.reader import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_CHARS,
    SUPPORTED_EXTENSIONS,
    DocumentReadResult,
    DocumentReader,
    DocumentReaderError,
)

__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_CHARS",
    "SUPPORTED_EXTENSIONS",
    "DocumentReadResult",
    "DocumentReader",
    "DocumentReaderError",
]
