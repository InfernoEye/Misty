"""Read-only, bounded text extraction from documents inside one approved root.

This module never writes, executes document content, or logs file contents.
HTML is parsed with BeautifulSoup's built-in ``html.parser``; scripts, styles,
and other non-visible elements are removed rather than evaluated. No Qt,
model, database, or desktop-automation modules are imported.

Text decoding policy:

1. UTF-8, tolerating a leading UTF-8 BOM.
2. Windows-1252, for common Western legacy encodings.
3. Latin-1, a total fallback that maps every byte to a character.

A NUL byte, or a high proportion of control bytes, marks the input as binary
and it is refused before decoding. UTF-16/UTF-32 are not supported because
their NUL bytes are indistinguishable from binary content under this policy.

Only regular files inside the approved root can be read. Parent traversal,
symlink escape, directories, devices, missing paths, oversized inputs, and
unsupported extensions all produce a failed :class:`DocumentReadResult`
instead of an exception. Construction-time configuration errors raise
:class:`DocumentReaderError`.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup

from app.actions.file_scope import FileScopeError, UserFolderResolver
from app.documents.converters import (
    ExternalConversionError,
    ExternalConverter,
)


DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_CHARS = 200_000

SUPPORTED_EXTENSIONS = frozenset({".txt", ".md", ".html", ".htm"})

_FORMAT_BY_EXTENSION = {
    ".txt": "text",
    ".md": "markdown",
    ".html": "html",
    ".htm": "html",
}

_EXTERNAL_FORMATS = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".odt": "odt",
}

# Extensions for which a human-readable "no library installed" message is
# more useful than a generic unsupported-format message.
_OFFICE_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".odt",
        ".ods",
        ".odp",
        ".rtf",
    }
)

_HTML_NOISE_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "iframe",
    "object",
    "embed",
    "svg",
    "canvas",
)

_WHITESPACE_RE = re.compile(r"[ \t\f\v]+")
_UTF8_BOM = b"\xef\xbb\xbf"


class DocumentReaderError(ValueError):
    """Raised when a reader is configured with an invalid root or limit."""


@dataclass(frozen=True, slots=True)
class DocumentReadResult:
    """Outcome of one read-only extraction attempt.

    ``format`` is ``"text"``, ``"markdown"``, ``"html"``, or ``None`` when the
    path could not be resolved or the extension is unsupported. ``error`` is a
    stable code suitable for tool routing; ``message`` is safe to show to a
    user and never contains a raw exception or document content.
    """

    ok: bool
    format: str | None = None
    text: str = ""
    error: str | None = None
    message: str = ""
    source_bytes: int = 0
    encoding: str | None = None
    text_truncated: bool = False

    @property
    def truncated(self) -> bool:
        """Whether the returned text was shortened to the output limit."""
        return self.text_truncated

    def as_dict(self) -> dict:
        """Return a JSON-friendly representation for tool integration."""
        return {
            "ok": self.ok,
            "format": self.format,
            "text": self.text,
            "error": self.error,
            "message": self.message,
            "source_bytes": self.source_bytes,
            "encoding": self.encoding,
            "truncated": self.truncated,
            "text_truncated": self.text_truncated,
        }


class DocumentReader:
    """Extract text from files inside ``allowed_root``.

    The root is resolved to a canonical absolute directory once, at
    construction. Every read is then confined to that directory, including
    through symlinks. Limits are validated at construction; oversized files
    are refused rather than partially read, while extracted text longer than
    ``max_chars`` is truncated and flagged.

    Example::

        reader = DocumentReader(Path("/home/user/Documents"))
        result = reader.read("notes/todo.md")
        if result.ok:
            print(result.text)
    """

    def __init__(
        self,
        allowed_root,
        *,
        max_bytes=DEFAULT_MAX_BYTES,
        max_chars=DEFAULT_MAX_CHARS,
        converter=None,
    ):
        self._max_bytes = self._limit(max_bytes, "max_bytes")
        self._max_chars = self._limit(max_chars, "max_chars")
        self._converter = converter or ExternalConverter()

        raw_root = self._coerce_path(allowed_root)
        if raw_root is None:
            raise DocumentReaderError(
                "An allowed root directory is required."
            )
        try:
            root = Path(raw_root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise DocumentReaderError(
                "The allowed root directory is unavailable."
            ) from None
        if not root.is_dir():
            raise DocumentReaderError(
                "The allowed root is not a directory."
            )

        self._root = root
        # Reuse Misty's canonical, symlink-aware path containment. The
        # resolver is only used through ``child``; no XDG lookup occurs.
        self._resolver = UserFolderResolver(
            home=root,
            xdg_lookup=lambda _name: None,
        )

    @property
    def allowed_root(self) -> Path:
        """Canonical absolute root that every read is confined to."""
        return self._root

    @property
    def max_bytes(self) -> int:
        """Largest accepted input file, in bytes."""
        return self._max_bytes

    @property
    def max_chars(self) -> int:
        """Largest returned text, in characters."""
        return self._max_chars

    def read(self, path) -> DocumentReadResult:
        """Read one file, returning a result instead of raising on failure.

        ``path`` may be relative to :attr:`allowed_root` or an absolute path
        lexically inside it. Any ``..`` component, absolute path outside the
        root, or symlink that resolves outside the root is rejected.
        """
        raw_path = self._coerce_path(path)
        if raw_path is None:
            return self._failure(
                "path_not_allowed",
                "An allowed file path is required.",
            )

        relative = self._relative_to_root(raw_path)
        if relative is None:
            return self._failure(
                "path_not_allowed",
                "The path is not allowed inside the approved root.",
            )

        try:
            target = self._resolver.child(self._root, relative.as_posix())
        except FileScopeError:
            error, message = self._classify_resolution_failure(relative)
            return self._failure(error, message)
        except (OSError, RuntimeError, ValueError):
            return self._failure(
                "path_not_allowed",
                "The path is not allowed inside the approved root.",
            )

        if not target.is_file():
            return self._failure(
                "not_a_file",
                "The requested path is not a regular file.",
            )

        external_format = _EXTERNAL_FORMATS.get(target.suffix.lower())
        if external_format is not None:
            return self._read_external(target, external_format)

        format_name = _FORMAT_BY_EXTENSION.get(target.suffix.lower())
        if format_name is None:
            message = (
                "This Office format is not supported for text extraction."
                if target.suffix.lower() in _OFFICE_EXTENSIONS
                else "This file format is not supported for text extraction."
            )
            return self._failure("unsupported_format", message)

        data, failure = self._read_bytes(target, format_name)
        if failure is not None:
            return failure

        source_bytes = len(data)

        if self._looks_binary(data):
            return self._failure(
                "binary_content",
                "The file appears to be binary, not text.",
                format=format_name,
                source_bytes=source_bytes,
            )

        decoded = self._decode(data)
        if decoded is None:
            return self._failure(
                "decode_error",
                "The file text could not be decoded.",
                format=format_name,
                source_bytes=source_bytes,
            )
        text, encoding = decoded
        text = text.replace("\r\n", "\n").replace("\r", "\n")

        try:
            if format_name == "html":
                text = self._html_to_text(text)
        except Exception:
            return self._failure(
                "parse_error",
                "The document could not be parsed as HTML.",
                format=format_name,
                source_bytes=source_bytes,
                encoding=encoding,
            )

        truncated = len(text) > self._max_chars
        if truncated:
            text = text[: self._max_chars]

        return DocumentReadResult(
            ok=True,
            format=format_name,
            text=text,
            source_bytes=source_bytes,
            encoding=encoding,
            text_truncated=truncated,
        )

    def _read_external(self, target, format_name):
        """Convert PDF/DOCX/ODT through a fixed local executable."""
        try:
            size = target.stat().st_size
        except OSError:
            return self._failure(
                "read_error",
                "The file could not be read.",
                format=format_name,
            )
        if size > self._max_bytes:
            return self._failure(
                "file_too_large",
                "The file exceeds the maximum allowed input size.",
                format=format_name,
                source_bytes=size,
            )

        try:
            text, truncated, _converter = self._converter.extract(
                target, format_name, self._max_chars
            )
        except ExternalConversionError as error:
            return self._failure(
                "conversion_failed",
                str(error),
                format=format_name,
                source_bytes=size,
            )

        return DocumentReadResult(
            ok=True,
            format=format_name,
            text=text,
            source_bytes=size,
            encoding="utf-8",
            text_truncated=truncated,
        )

    def _read_bytes(self, target, format_name):
        """Open securely and read at most ``max_bytes`` plus one probe byte."""
        flags = os.O_RDONLY
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)

        try:
            descriptor = os.open(target, flags)
        except (OSError, ValueError):
            return b"", self._failure(
                "read_error",
                "The file could not be read.",
                format=format_name,
            )

        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                return b"", self._failure(
                    "not_a_file",
                    "The requested path is not a regular file.",
                )
            if info.st_size > self._max_bytes:
                return b"", self._failure(
                    "file_too_large",
                    "The file exceeds the maximum allowed input size.",
                    format=format_name,
                    source_bytes=info.st_size,
                )

            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                data = handle.read(self._max_bytes + 1)
        except (OSError, ValueError):
            return b"", self._failure(
                "read_error",
                "The file could not be read.",
                format=format_name,
            )
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

        if len(data) > self._max_bytes:
            # The file grew after it was inspected.
            return b"", self._failure(
                "file_too_large",
                "The file exceeds the maximum allowed input size.",
                format=format_name,
                source_bytes=len(data),
            )
        return data, None

    def _relative_to_root(self, raw_path):
        """Return a safe relative path, or ``None`` if it is disallowed."""
        try:
            path = Path(raw_path)
        except (TypeError, ValueError):
            return None

        if path.is_absolute():
            try:
                relative = path.relative_to(self._root)
            except ValueError:
                return None
        else:
            relative = path

        if ".." in relative.parts:
            return None
        return relative if relative.parts else Path(".")

    def _classify_resolution_failure(self, relative):
        """Map a resolver refusal to a stable error code and safe message."""
        lexical = self._root / relative
        try:
            if not os.path.lexists(lexical):
                return (
                    "path_not_found",
                    "The requested file does not exist inside the "
                    "approved root.",
                )
            resolved = lexical.resolve(strict=False)
            if not resolved.is_relative_to(self._root):
                return (
                    "path_not_allowed",
                    "The requested path escapes the approved root.",
                )
            if lexical.is_dir() or resolved.is_dir():
                return (
                    "not_a_file",
                    "The requested path is not a regular file.",
                )
        except (OSError, RuntimeError, ValueError):
            pass
        return (
            "path_not_allowed",
            "The requested path is not an allowed file inside the "
            "approved root.",
        )

    @staticmethod
    def _decode(data):
        """Decode with the documented UTF-8, cp1252, latin-1 fallback chain."""
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            pass
        else:
            encoding = "utf-8-sig" if data.startswith(_UTF8_BOM) else "utf-8"
            return text, encoding

        for encoding in ("cp1252", "latin-1"):
            try:
                return data.decode(encoding), encoding
            except UnicodeDecodeError:
                continue
        return None

    @staticmethod
    def _looks_binary(data) -> bool:
        """Detect NUL bytes or a control-character-heavy byte stream."""
        if b"\x00" in data:
            return True
        sample = data[:8192]
        if not sample:
            return False
        control = sum(
            1
            for byte in sample
            if byte < 32 and byte not in (9, 10, 13)
        )
        return control / len(sample) > 0.10

    @classmethod
    def _html_to_text(cls, text) -> str:
        """Remove non-visible elements and return normalized readable text."""
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup.find_all(_HTML_NOISE_TAGS):
            tag.decompose()

        container = soup.body if soup.body is not None else soup
        return cls._normalize_whitespace(container.get_text(separator="\n"))

    @staticmethod
    def _normalize_whitespace(text) -> str:
        """Collapse runs of spaces and excess blank lines in HTML output."""
        text = text.replace("\xa0", " ")
        lines = []
        blank_lines = 0
        for raw_line in text.split("\n"):
            line = _WHITESPACE_RE.sub(" ", raw_line).strip()
            if line:
                blank_lines = 0
                lines.append(line)
            else:
                if blank_lines == 0:
                    lines.append("")
                blank_lines += 1
        return "\n".join(lines).strip()

    @staticmethod
    def _coerce_path(value):
        """Accept str/PathLike input without bytes or embedded NUL."""
        if not isinstance(value, (str, os.PathLike)):
            return None
        try:
            raw = os.fspath(value)
        except TypeError:
            return None
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            return None
        return raw

    @staticmethod
    def _limit(value, name):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DocumentReaderError(
                f"{name} must be a positive integer."
            )
        return value

    @staticmethod
    def _failure(
        error,
        message,
        *,
        format=None,
        source_bytes=0,
        encoding=None,
    ):
        return DocumentReadResult(
            ok=False,
            format=format,
            text="",
            error=error,
            message=message,
            source_bytes=source_bytes,
            encoding=encoding,
        )
