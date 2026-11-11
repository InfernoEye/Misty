"""Isolated audit log and task checkpoint journal for Misty.

This module is intentionally self-contained.  It has no Qt, model, tool or
application imports, so it can be reviewed and tested before integration.

Two independent facilities are provided:

``AuditLog``
    Append-only JSON Lines records with explicit fields, short redacted
    summaries, size-based rotation and restrictive permissions.

``TaskJournal``
    A small SQLite journal for task lifecycle and safe, non-sensitive
    checkpoints.

Security rules enforced here:

* Audit records have no content fields.  Only a short caller-supplied summary
  is stored, scrubbed of known sensitive assignments and capped.  Callers must
  never pass raw clipboard, OCR, document, memory, prompt or model text.
* Known sensitive mapping keys are removed by :func:`redact_fields` and are
  rejected inside task checkpoints.
* Exception details are not returned to callers.  Failures are reported as a
  structured result with a stable error code and a fixed safe message.

This module does not sign records and does not attempt to detect tampering.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import sqlite3
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

__all__ = [
    "AuditLog",
    "AuditLogError",
    "AuditLogResult",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_FILES",
    "LOG_FILENAME",
    "MAX_ACTION_LENGTH",
    "MAX_CHECKPOINT_BYTES",
    "MAX_CHECKPOINT_DEPTH",
    "MAX_CHECKPOINT_ITEMS",
    "MAX_CHECKPOINT_NODES",
    "MAX_CHECKPOINT_STRING",
    "MAX_ERROR_CODE_LENGTH",
    "MAX_EVENT_TYPE_LENGTH",
    "MAX_REQUEST_ID_LENGTH",
    "MAX_REQUEST_LABEL_LENGTH",
    "MAX_STATUS_LENGTH",
    "MAX_SUMMARY_LENGTH",
    "MAX_TASK_ID_LENGTH",
    "SENSITIVE_KEYS",
    "TASK_STATUSES",
    "TaskJournal",
    "TaskJournalError",
    "TaskJournalResult",
    "is_sensitive_key",
    "redact_fields",
    "redact_summary",
]


LOG_FILENAME = "audit.jsonl"
ARCHIVE_PREFIX = "audit-"
ARCHIVE_SUFFIX = ".jsonl"

DEFAULT_MAX_BYTES = 1_000_000
DEFAULT_MAX_FILES = 5

MAX_SUMMARY_LENGTH = 500
MAX_EVENT_TYPE_LENGTH = 64
MAX_ACTION_LENGTH = 120
MAX_STATUS_LENGTH = 32
MAX_REQUEST_ID_LENGTH = 64
MAX_ERROR_CODE_LENGTH = 64
MAX_TASK_ID_LENGTH = 128

#: Keys that must never be persisted by the audit facilities.  The list covers
#: the explicitly required names plus obvious aliases for the same content.
SENSITIVE_KEYS = frozenset(
    {
        "text",
        "content",
        "clipboard",
        "ocr_text",
        "visual_interpretation",
        "arguments",
        "result",
        "password",
        "token",
        "secret",
        "api_key",
        "raw_text",
        "transcript",
        "speech",
        "document",
        "document_text",
        "memory",
        "memory_content",
        "authorization",
        "credentials",
        "access_token",
        "refresh_token",
        "private_key",
        "cookie",
        "user_text",
        "user_speech",
        "ocr",
        "image",
        "screenshot",
        "prompt",
        "prompt_text",
        "completion",
        "completion_text",
        "audio",
        "clipboard_text",
        "secret_key",
        "auth_token",
        "session_token",
        "passwd",
        "passphrase",
        "credential",
    }
)

# Components make names such as ``user_token`` or ``clipboard_text`` sensitive
# even when the exact key is not listed above.
_SENSITIVE_COMPONENTS = frozenset(
    {
        "text",
        "content",
        "clipboard",
        "ocr",
        "result",
        "arguments",
        "password",
        "passwd",
        "passphrase",
        "secret",
        "token",
        "credential",
        "credentials",
        "authorization",
        "cookie",
        "transcript",
        "speech",
        "memory",
        "document",
        "prompt",
        "completion",
        "image",
        "screenshot",
        "audio",
    }
)

_TASK_ID_RE = re.compile(
    rf"^[A-Za-z0-9][A-Za-z0-9_.:-]{{0,{MAX_TASK_ID_LENGTH - 1}}}$"
)
_EVENT_TYPE_RE = re.compile(
    rf"^[A-Za-z0-9][A-Za-z0-9_.:-]{{0,{MAX_EVENT_TYPE_LENGTH - 1}}}$"
)
_STATUS_RE = re.compile(
    rf"^[a-z][a-z0-9_.:-]{{0,{MAX_STATUS_LENGTH - 1}}}$"
)
_REQUEST_ID_RE = re.compile(
    rf"^[A-Za-z0-9][A-Za-z0-9_.:-]{{0,{MAX_REQUEST_ID_LENGTH - 1}}}$"
)
_ERROR_CODE_RE = re.compile(
    rf"^[A-Za-z0-9][A-Za-z0-9_.:-]{{0,{MAX_ERROR_CODE_LENGTH - 1}}}$"
)
_ARCHIVE_RE = re.compile(
    rf"^{re.escape(ARCHIVE_PREFIX)}"
    r"\d{8}T\d{6}\.\d{6}Z-\d{8}-[0-9a-f]{8}"
    rf"{re.escape(ARCHIVE_SUFFIX)}$"
)

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+"
)
_TRUNCATION_SUFFIX = "...[truncated]"

MAX_REQUEST_LABEL_LENGTH = 160

MAX_CHECKPOINT_BYTES = 8_192
MAX_CHECKPOINT_DEPTH = 4
MAX_CHECKPOINT_ITEMS = 50
MAX_CHECKPOINT_NODES = 256
MAX_CHECKPOINT_STRING = 1_000
MAX_CHECKPOINT_KEY_LENGTH = 64

TASK_STATUSES = frozenset(
    {"pending", "running", "paused", "completed", "failed", "cancelled"}
)
_START_STATUSES = frozenset({"pending", "running"})
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_STATUS_ALIASES = {"canceled": "cancelled"}

_REDACTED = "[redacted]"


class AuditLogError(RuntimeError):
    """Raised when the audit log cannot be constructed."""


class TaskJournalError(RuntimeError):
    """Raised when the task journal cannot be constructed."""


class _Rejected(Exception):
    """Internal validation failure; never exposed to callers directly."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class AuditLogResult:
    """Structured outcome of :meth:`AuditLog.log_event`."""

    ok: bool
    record: dict[str, Any] | None = None
    error_code: str | None = None
    error: str | None = None
    archived_to: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskJournalResult:
    """Structured outcome of a task journal mutation."""

    ok: bool
    task: dict[str, Any] | None = None
    error_code: str | None = None
    error: str | None = None


def _utc_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _best_effort_chmod(path: Path, mode: int) -> bool:
    """Set *mode* on POSIX; silently ignore unsupported platforms/errors."""

    if os.name != "posix":
        return False
    try:
        os.chmod(path, mode)
        return True
    except OSError:
        return False


def _clean_text(value: str) -> str:
    """Make text safe for one-line display and valid UTF-8."""

    text = value.encode("utf-8", "replace").decode("utf-8")
    text = _CONTROL_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _safe_utf8(value: str) -> str:
    """Replace lone surrogates while preserving whitespace and controls."""

    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return value.encode("utf-8", "replace").decode("utf-8")
    return value


def _truncate(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    if max_length <= len(_TRUNCATION_SUFFIX):
        return _TRUNCATION_SUFFIX[:max_length]
    return text[: max_length - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX


def _normalize_key(key: str) -> str:
    spaced = key.strip()
    # Split acronym boundaries (OCRText -> OCR_Text) and camelCase.
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", spaced)
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", spaced)
    return re.sub(r"[^a-z0-9]+", "_", spaced.lower()).strip("_")


def is_sensitive_key(key: object) -> bool:
    """Return True when *key* names content that must not be persisted."""

    if not isinstance(key, str):
        return False
    normalized = _normalize_key(key)
    if not normalized:
        return False
    if normalized in SENSITIVE_KEYS:
        return True
    return any(
        part in _SENSITIVE_COMPONENTS for part in normalized.split("_")
    )


def _key_pattern(key: str) -> str:
    # Match both snake_case and hyphen/space variants of a sensitive key.
    return re.escape(key).replace("_", "[ _-]?")


_SENSITIVE_KEY_PATTERN = "|".join(
    _key_pattern(key) for key in sorted(SENSITIVE_KEYS, key=len, reverse=True)
)
_ASSIGNMENT_KEY_RE = re.compile(
    rf"(?i)(?P<key>\b(?:{_SENSITIVE_KEY_PATTERN})\b)[\"']?\s*[:=]\s*"
)

_JSON_PAIRS = {"{": "}", "[": "]"}


def _scan_structured_value(text: str, start: int) -> int | None:
    """Return the end of a balanced JSON-like object/array, or None."""

    stack = [_JSON_PAIRS[text[start]]]
    quote: str | None = None
    escaped = False
    index = start + 1
    while index < len(text):
        character = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
        else:
            if character in "\"'":
                quote = character
            elif character in _JSON_PAIRS:
                stack.append(_JSON_PAIRS[character])
            elif character in "}]":
                if not stack or character != stack[-1]:
                    return None
                stack.pop()
                if not stack:
                    return index + 1
        index += 1
    return None


def _sensitive_value_end(text: str, start: int) -> int:
    """Find the end of the value belonging to a sensitive assignment."""

    if start >= len(text):
        return start
    character = text[start]

    if character in _JSON_PAIRS:
        end = _scan_structured_value(text, start)
        # Unbalanced input is redacted to the end rather than left exposed.
        return len(text) if end is None else end

    if character in "\"'":
        quote = character
        escaped = False
        index = start + 1
        while index < len(text):
            current = text[index]
            if escaped:
                escaped = False
            elif current == "\\":
                escaped = True
            elif current == quote:
                return index + 1
            index += 1
        return len(text)

    # Unquoted values can contain spaces (passphrases).  Redact through the
    # next sensitive assignment, or to the end, rather than leaving trailing
    # words of a secret exposed.
    next_assignment = _ASSIGNMENT_KEY_RE.search(text, start)
    return next_assignment.start() if next_assignment else len(text)


def _redact_assignments(text: str) -> str:
    pieces: list[str] = []
    position = 0
    while True:
        match = _ASSIGNMENT_KEY_RE.search(text, position)
        if match is None:
            break
        value_end = _sensitive_value_end(text, match.end())
        pieces.append(text[position : match.start()])
        pieces.append(f"{match.group('key')}={_REDACTED}")
        position = value_end
    pieces.append(text[position:])
    return "".join(pieces)


def redact_summary(value: object, max_length: int = MAX_SUMMARY_LENGTH) -> str:
    """Return a short, single-line summary with obvious secrets removed.

    This is a defence-in-depth helper, not a content classifier.  Callers must
    still never pass raw clipboard, OCR, document, memory or model text.  The
    helper removes ``key=value`` style assignments for sensitive names,
    authorization header values, collapses whitespace and caps the length.
    """

    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError("summary must be a string")
    if (
        isinstance(max_length, bool)
        or not isinstance(max_length, int)
        or max_length <= 0
    ):
        raise ValueError("max_length must be a positive integer")

    # Bound the input before cleaning and running regular expressions.
    cap = max_length * 4
    text = _clean_text(value[:cap])
    if len(text) > cap:
        text = text[:cap]

    text = _AUTH_HEADER_RE.sub(r"\1 [redacted]", text)
    text = _redact_assignments(text)
    return _truncate(text, max_length)


def redact_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Return a flat copy of *fields* without sensitive or nested values.

    Sensitive keys and non-string keys are removed.  Strings are passed through
    :func:`redact_summary`; scalars are retained; mappings, sequences, bytes and
    other objects are dropped because this helper never accepts nested payloads.
    """

    if not isinstance(fields, Mapping):
        raise TypeError("fields must be a mapping")

    safe: dict[str, Any] = {}
    for key, value in fields.items():
        if not isinstance(key, str) or len(key) > 64:
            continue
        if is_sensitive_key(key):
            continue
        if isinstance(value, str):
            safe[key] = redact_summary(value)
        elif value is None or isinstance(value, bool):
            safe[key] = value
        elif isinstance(value, int):
            safe[key] = value
        elif isinstance(value, float) and math.isfinite(value):
            safe[key] = value
        # Everything else (including nested containers) is intentionally
        # dropped rather than serialized.
    return safe


def _validate_task_id(value: object) -> str:
    if not isinstance(value, str):
        raise _Rejected("invalid_task_id", "task_id must be a string")
    task_id = value.strip()
    if not _TASK_ID_RE.fullmatch(task_id):
        raise _Rejected(
            "invalid_task_id",
            "task_id must be 1-128 safe identifier characters",
        )
    return task_id


def _validate_request_label(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _Rejected(
            "invalid_request_label", "request_label must be a string"
        )
    return _truncate(
        redact_summary(value, max_length=MAX_REQUEST_LABEL_LENGTH),
        MAX_REQUEST_LABEL_LENGTH,
    )


def _normalize_task_status(value: object) -> str:
    if not isinstance(value, str):
        raise _Rejected("invalid_status", "status must be a string")
    normalized = value.strip().lower()
    normalized = _STATUS_ALIASES.get(normalized, normalized)
    if normalized not in TASK_STATUSES:
        raise _Rejected("invalid_status", "unsupported task status")
    return normalized


def _checkpoint_json(checkpoint: object) -> str:
    if checkpoint is None:
        checkpoint = {}
    if not isinstance(checkpoint, Mapping):
        raise _Rejected(
            "invalid_checkpoint", "checkpoint must be a JSON object"
        )

    normalized = _normalize_json(
        checkpoint, depth=0, seen=set(), budget=[0]
    )
    try:
        text = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        encoded = text.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise _Rejected(
            "invalid_checkpoint", "checkpoint is not JSON serializable"
        ) from None

    if len(encoded) > MAX_CHECKPOINT_BYTES:
        raise _Rejected(
            "checkpoint_too_large", "checkpoint exceeds the size limit"
        )
    return text


def _normalize_json(
    value: object, *, depth: int, seen: set[int], budget: list[int]
) -> Any:
    budget[0] += 1
    if budget[0] > MAX_CHECKPOINT_NODES:
        raise _Rejected(
            "checkpoint_too_large", "checkpoint contains too many values"
        )
    if depth > MAX_CHECKPOINT_DEPTH:
        raise _Rejected(
            "invalid_checkpoint", "checkpoint is nested too deeply"
        )

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = _safe_utf8(value)
        if len(text) > MAX_CHECKPOINT_STRING:
            raise _Rejected(
                "checkpoint_too_large", "checkpoint string is too long"
            )
        return text
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _Rejected(
                "invalid_checkpoint",
                "checkpoint contains a non-finite number",
            )
        return value

    if isinstance(value, Mapping):
        if len(value) > MAX_CHECKPOINT_ITEMS:
            raise _Rejected(
                "checkpoint_too_large", "checkpoint object has too many keys"
            )
        marker = id(value)
        if marker in seen:
            raise _Rejected(
                "invalid_checkpoint", "checkpoint contains a cycle"
            )
        seen.add(marker)
        try:
            result: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise _Rejected(
                        "invalid_checkpoint",
                        "checkpoint keys must be strings",
                    )
                clean_key = _safe_utf8(key)
                if not clean_key or len(clean_key) > MAX_CHECKPOINT_KEY_LENGTH:
                    raise _Rejected(
                        "invalid_checkpoint", "checkpoint key is invalid"
                    )
                if is_sensitive_key(clean_key):
                    raise _Rejected(
                        "sensitive_checkpoint_key",
                        "checkpoint contains a sensitive field",
                    )
                result[clean_key] = _normalize_json(
                    item,
                    depth=depth + 1,
                    seen=seen,
                    budget=budget,
                )
            return result
        finally:
            seen.discard(marker)

    if isinstance(value, list):
        if len(value) > MAX_CHECKPOINT_ITEMS:
            raise _Rejected(
                "checkpoint_too_large", "checkpoint list has too many items"
            )
        marker = id(value)
        if marker in seen:
            raise _Rejected(
                "invalid_checkpoint", "checkpoint contains a cycle"
            )
        seen.add(marker)
        try:
            return [
                _normalize_json(
                    item,
                    depth=depth + 1,
                    seen=seen,
                    budget=budget,
                )
                for item in value
            ]
        finally:
            seen.discard(marker)

    raise _Rejected(
        "invalid_checkpoint", "checkpoint contains an unsupported value"
    )


class AuditLog:
    """Thread-safe append-only JSON Lines audit log with rotation."""

    def __init__(
        self,
        directory: str | os.PathLike[str],
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_files: int = DEFAULT_MAX_FILES,
    ) -> None:
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
        ):
            raise ValueError("max_bytes must be a positive integer")
        if (
            isinstance(max_files, bool)
            or not isinstance(max_files, int)
            or max_files < 1
        ):
            raise ValueError("max_files must be a positive integer")

        self.directory = Path(directory)
        self.max_bytes = max_bytes
        self.max_files = max_files
        self.path = self.directory / LOG_FILENAME
        self._lock = threading.Lock()
        self._handle: Any = None
        self._closed = False
        self._rotation_sequence = 0

        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise AuditLogError("audit directory could not be created") from None
        if not self.directory.is_dir():
            raise AuditLogError("audit directory is not a directory")
        _best_effort_chmod(self.directory, 0o700)

    # -- public API -----------------------------------------------------

    def log_event(
        self,
        event_type: str,
        action: str,
        status: str,
        *,
        request_id: str | None = None,
        duration_ms: int | float | None = None,
        summary: str | None = "",
        error_code: str | None = None,
    ) -> AuditLogResult:
        """Append one audit record and return a structured result.

        Only the explicit fields above are accepted.  There is deliberately no
        ``metadata`` or arbitrary payload parameter.  ``summary`` must be a
        short label, never clipboard/OCR/document/memory/model content; the
        pattern-based scrubber is defence in depth, not a content classifier.
        """

        try:
            record = self._build_record(
                event_type=event_type,
                action=action,
                status=status,
                request_id=request_id,
                duration_ms=duration_ms,
                summary=summary,
                error_code=error_code,
            )
            line = json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ) + "\n"
            data = line.encode("utf-8")
        except _Rejected as exc:
            return self._failure(exc.code, exc.message)
        except (TypeError, ValueError, UnicodeEncodeError):
            return self._failure(
                "invalid_record", "audit record could not be encoded"
            )

        with self._lock:
            if self._closed:
                return self._failure("log_closed", "audit log is closed")

            try:
                self._open_locked()
            except OSError:
                return self._failure(
                    "write_failed", "audit record could not be written"
                )

            archived_to: str | None = None
            warnings: tuple[str, ...] = ()
            try:
                size = os.fstat(self._handle.fileno()).st_size
                if size > 0 and size + len(data) > self.max_bytes:
                    (
                        archived_to,
                        rotation_issue,
                        prune_warnings,
                    ) = self._rotate_locked()
                    warnings = prune_warnings
                    if rotation_issue == "write_failed":
                        return self._failure(
                            "write_failed",
                            "audit log could not be rotated",
                        )
                    if rotation_issue == "rotation_failed":
                        warnings = warnings + ("rotation_failed",)

                self._handle.write(line)
                self._handle.flush()
            except (OSError, ValueError):
                self._close_handle_locked()
                return self._failure(
                    "write_failed", "audit record could not be written"
                )

        return AuditLogResult(
            ok=True,
            record=record,
            archived_to=archived_to,
            warnings=warnings,
        )

    def read_records(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Read valid records from the active file (archives excluded).

        Malformed lines are skipped rather than raising.  ``limit`` returns the
        most recent *limit* records.
        """

        if limit is not None:
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or limit < 0
            ):
                return []
            if limit == 0:
                return []

        with self._lock:
            if self._handle is not None:
                try:
                    self._handle.flush()
                except (OSError, ValueError):
                    pass
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    lines = handle.readlines()
            except (OSError, UnicodeError):
                return []

        records: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, UnicodeError):
                continue
            if isinstance(value, dict):
                records.append(value)

        if limit is not None:
            records = records[-limit:]
        return records

    def archives(self) -> list[Path]:
        """Return archive paths, oldest first, without touching unrelated files."""

        with self._lock:
            return self._list_archives_locked()

    def close(self) -> None:
        """Flush and close the active file.  Further writes are rejected."""

        with self._lock:
            self._closed = True
            self._close_handle_locked()

    def __enter__(self) -> "AuditLog":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    # -- validation -----------------------------------------------------

    def _build_record(
        self,
        *,
        event_type: object,
        action: object,
        status: object,
        request_id: object,
        duration_ms: object,
        summary: object,
        error_code: object,
    ) -> dict[str, Any]:
        if not isinstance(event_type, str):
            raise _Rejected(
                "invalid_event_type", "event_type must be a string"
            )
        normalized_event_type = event_type.strip()
        if not _EVENT_TYPE_RE.fullmatch(normalized_event_type):
            raise _Rejected(
                "invalid_event_type",
                "event_type must be 1-64 safe identifier characters",
            )

        if not isinstance(action, str):
            raise _Rejected("invalid_action", "action must be a string")
        cleaned_action = _clean_text(action)
        if not cleaned_action or len(cleaned_action) > MAX_ACTION_LENGTH:
            raise _Rejected(
                "invalid_action",
                "action must be 1-120 characters",
            )
        normalized_action = redact_summary(
            cleaned_action, max_length=MAX_ACTION_LENGTH
        )

        if not isinstance(status, str):
            raise _Rejected("invalid_status", "status must be a string")
        normalized_status = status.strip().lower()
        if not _STATUS_RE.fullmatch(normalized_status):
            raise _Rejected(
                "invalid_status",
                "status must be a short lowercase identifier",
            )

        normalized_request_id: str | None
        if request_id is None:
            normalized_request_id = None
        else:
            if not isinstance(request_id, str):
                raise _Rejected(
                    "invalid_request_id", "request_id must be a string"
                )
            candidate = request_id.strip()
            if not candidate:
                normalized_request_id = None
            elif not _REQUEST_ID_RE.fullmatch(candidate):
                raise _Rejected(
                    "invalid_request_id",
                    "request_id must be at most 64 safe identifier characters",
                )
            else:
                normalized_request_id = candidate

        if duration_ms is None:
            normalized_duration: int | None = None
        elif isinstance(duration_ms, bool) or not isinstance(
            duration_ms, (int, float)
        ):
            raise _Rejected(
                "invalid_duration_ms",
                "duration_ms must be a non-negative number",
            )
        elif isinstance(duration_ms, float):
            if not math.isfinite(duration_ms) or duration_ms < 0:
                raise _Rejected(
                    "invalid_duration_ms",
                    "duration_ms must be a non-negative number",
                )
            normalized_duration = int(round(duration_ms))
        else:
            if duration_ms < 0:
                raise _Rejected(
                    "invalid_duration_ms",
                    "duration_ms must be a non-negative number",
                )
            normalized_duration = duration_ms

        if summary is None:
            normalized_summary = ""
        elif not isinstance(summary, str):
            raise _Rejected("invalid_summary", "summary must be a string")
        else:
            try:
                normalized_summary = redact_summary(summary)
            except (TypeError, ValueError):
                raise _Rejected(
                    "invalid_summary", "summary must be a short string"
                ) from None

        normalized_error_code: str | None
        if error_code is None:
            normalized_error_code = None
        else:
            if not isinstance(error_code, str):
                raise _Rejected(
                    "invalid_error_code", "error_code must be a string"
                )
            candidate = error_code.strip()
            if not candidate:
                normalized_error_code = None
            elif not _ERROR_CODE_RE.fullmatch(candidate):
                raise _Rejected(
                    "invalid_error_code",
                    "error_code must be at most 64 safe identifier characters",
                )
            else:
                normalized_error_code = candidate

        return {
            "timestamp": _utc_timestamp(),
            "event_type": normalized_event_type,
            "action": normalized_action,
            "status": normalized_status,
            "request_id": normalized_request_id,
            "duration_ms": normalized_duration,
            "summary": normalized_summary,
            "error_code": normalized_error_code,
        }

    @staticmethod
    def _failure(code: str, message: str) -> AuditLogResult:
        return AuditLogResult(ok=False, error_code=code, error=message)

    # -- file handling --------------------------------------------------

    def _open_locked(self) -> None:
        if self._handle is not None:
            return
        descriptor = os.open(
            self.path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            0o600,
        )
        try:
            handle = os.fdopen(
                descriptor, "a", encoding="utf-8", newline="\n"
            )
        except Exception:
            os.close(descriptor)
            raise
        self._handle = handle
        _best_effort_chmod(self.path, 0o600)

    def _close_handle_locked(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.flush()
        except (OSError, ValueError):
            pass
        try:
            handle.close()
        except (OSError, ValueError):
            pass

    def _new_archive_path_locked(self) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        for _ in range(100):
            sequence = self._rotation_sequence
            self._rotation_sequence += 1
            name = (
                f"{ARCHIVE_PREFIX}{stamp}-{sequence:08d}-"
                f"{secrets.token_hex(4)}{ARCHIVE_SUFFIX}"
            )
            candidate = self.directory / name
            if not candidate.exists():
                return candidate
        # Practically unreachable; add entropy rather than overwrite anything.
        return self.directory / (
            f"{ARCHIVE_PREFIX}{stamp}-{self._rotation_sequence:08d}-"
            f"{secrets.token_hex(16)}{ARCHIVE_SUFFIX}"
        )

    def _list_archives_locked(self) -> list[Path]:
        try:
            entries = list(os.scandir(self.directory))
        except OSError:
            return []
        archives: list[Path] = []
        for entry in entries:
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                if not _ARCHIVE_RE.fullmatch(entry.name):
                    continue
            except OSError:
                continue
            archives.append(Path(entry.path))
        return sorted(archives, key=lambda path: path.name)

    def _prune_locked(self) -> tuple[str, ...]:
        archives = self._list_archives_locked()
        excess = len(archives) - self.max_files
        if excess <= 0:
            return ()
        warnings: list[str] = []
        for archive in archives[:excess]:
            try:
                archive.unlink()
            except OSError:
                warnings.append("prune_failed")
        return tuple(warnings)

    def _rotate_locked(
        self,
    ) -> tuple[str | None, str | None, tuple[str, ...]]:
        """Rotate the active file.  Return (archive, issue, warnings).

        ``issue`` is ``"rotation_failed"`` when logging can safely continue in
        the original file, or ``"write_failed"`` when no usable file remains.
        """

        self._close_handle_locked()
        archive = self._new_archive_path_locked()
        try:
            os.replace(self.path, archive)
        except OSError:
            try:
                self._open_locked()
            except OSError:
                return None, "write_failed", ()
            return None, "rotation_failed", ()

        try:
            self._open_locked()
        except OSError:
            # The previous data is safe in the archive.  Do not overwrite it by
            # trying to roll back blindly; report a safe failure instead.
            return str(archive), "write_failed", ()

        warnings = self._prune_locked()
        return str(archive), None, warnings


@contextmanager
def _sqlite_connection(database: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(database, timeout=10)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        with connection:
            yield connection
    finally:
        connection.close()


class TaskJournal:
    """Thread-safe SQLite task journal with non-sensitive checkpoints."""

    def __init__(self, database_path: str | os.PathLike[str]) -> None:
        self.database = Path(database_path)
        self._lock = threading.Lock()

        parent = self.database.parent
        try:
            created = not parent.exists()
            parent.mkdir(parents=True, exist_ok=True)
            if created:
                _best_effort_chmod(parent, 0o700)
        except OSError:
            raise TaskJournalError(
                "task journal directory could not be created"
            ) from None

        try:
            self._initialize()
        except sqlite3.Error:
            raise TaskJournalError(
                "task journal database could not be initialized"
            ) from None
        _best_effort_chmod(self.database, 0o600)

    def _initialize(self) -> None:
        with self._lock, _sqlite_connection(self.database) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task_journal (
                    task_id TEXT PRIMARY KEY,
                    request_label TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    checkpoint_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_task_journal_updated
                ON task_journal (updated_at DESC)
                """
            )

    # -- mutations ------------------------------------------------------

    def start_task(
        self,
        task_id: str,
        request_label: str | None = None,
        *,
        status: str = "running",
        checkpoint: Mapping[str, Any] | None = None,
    ) -> TaskJournalResult:
        """Create a task.  ``request_label`` must be a short safe label.

        Raw user speech or message text must never be passed here.  The label is
        redacted and truncated before it is stored.
        """

        try:
            normalized_id = _validate_task_id(task_id)
            label = _validate_request_label(request_label)
            normalized_status = _normalize_task_status(status)
            if normalized_status not in _START_STATUSES:
                raise _Rejected(
                    "invalid_status",
                    "task can only start as pending or running",
                )
            checkpoint_text = _checkpoint_json(checkpoint)
        except _Rejected as exc:
            return self._failure(exc.code, exc.message)

        timestamp = _utc_timestamp()
        try:
            with self._lock, _sqlite_connection(self.database) as connection:
                try:
                    connection.execute(
                        """
                        INSERT INTO task_journal
                            (task_id, request_label, status, created_at,
                             updated_at, checkpoint_json)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            normalized_id,
                            label,
                            normalized_status,
                            timestamp,
                            timestamp,
                            checkpoint_text,
                        ),
                    )
                except sqlite3.IntegrityError:
                    return self._failure(
                        "task_exists", "task already exists"
                    )
                row = connection.execute(
                    "SELECT * FROM task_journal WHERE task_id = ?",
                    (normalized_id,),
                ).fetchone()
        except sqlite3.Error:
            return self._failure(
                "database_error", "task journal is unavailable"
            )

        if row is None:
            return self._failure(
                "database_error", "task journal is unavailable"
            )
        return TaskJournalResult(ok=True, task=_row_to_task(row))

    def checkpoint(
        self,
        task_id: str,
        checkpoint: Mapping[str, Any] | None,
        *,
        status: str | None = None,
    ) -> TaskJournalResult:
        """Replace a task checkpoint (and optionally its status)."""

        try:
            normalized_id = _validate_task_id(task_id)
            checkpoint_text = _checkpoint_json(checkpoint)
            normalized_status = (
                None if status is None else _normalize_task_status(status)
            )
        except _Rejected as exc:
            return self._failure(exc.code, exc.message)

        timestamp = _utc_timestamp()
        try:
            with self._lock, _sqlite_connection(self.database) as connection:
                if normalized_status is None:
                    connection.execute(
                        """
                        UPDATE task_journal
                        SET checkpoint_json = ?, updated_at = ?
                        WHERE task_id = ?
                        """,
                        (checkpoint_text, timestamp, normalized_id),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE task_journal
                        SET status = ?, checkpoint_json = ?, updated_at = ?
                        WHERE task_id = ?
                        """,
                        (
                            normalized_status,
                            checkpoint_text,
                            timestamp,
                            normalized_id,
                        ),
                    )
                row = connection.execute(
                    "SELECT * FROM task_journal WHERE task_id = ?",
                    (normalized_id,),
                ).fetchone()
        except sqlite3.Error:
            return self._failure(
                "database_error", "task journal is unavailable"
            )

        if row is None:
            return self._failure("not_found", "task was not found")
        return TaskJournalResult(ok=True, task=_row_to_task(row))

    def finish_task(
        self,
        task_id: str,
        *,
        status: str = "completed",
        checkpoint: Mapping[str, Any] | None = None,
    ) -> TaskJournalResult:
        """Mark a task finished, optionally replacing its final checkpoint."""

        try:
            normalized_id = _validate_task_id(task_id)
            normalized_status = _normalize_task_status(status)
            if normalized_status not in _TERMINAL_STATUSES:
                raise _Rejected(
                    "invalid_status",
                    "finish status must be completed, failed or cancelled",
                )
            checkpoint_text = (
                None if checkpoint is None else _checkpoint_json(checkpoint)
            )
        except _Rejected as exc:
            return self._failure(exc.code, exc.message)

        timestamp = _utc_timestamp()
        try:
            with self._lock, _sqlite_connection(self.database) as connection:
                if checkpoint_text is None:
                    connection.execute(
                        """
                        UPDATE task_journal
                        SET status = ?, updated_at = ?
                        WHERE task_id = ?
                        """,
                        (normalized_status, timestamp, normalized_id),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE task_journal
                        SET status = ?, checkpoint_json = ?, updated_at = ?
                        WHERE task_id = ?
                        """,
                        (
                            normalized_status,
                            checkpoint_text,
                            timestamp,
                            normalized_id,
                        ),
                    )
                row = connection.execute(
                    "SELECT * FROM task_journal WHERE task_id = ?",
                    (normalized_id,),
                ).fetchone()
        except sqlite3.Error:
            return self._failure(
                "database_error", "task journal is unavailable"
            )

        if row is None:
            return self._failure("not_found", "task was not found")
        return TaskJournalResult(ok=True, task=_row_to_task(row))

    # -- reads ----------------------------------------------------------

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Return a task dictionary or None (including for invalid IDs)."""

        try:
            normalized_id = _validate_task_id(task_id)
        except _Rejected:
            return None

        try:
            with self._lock, _sqlite_connection(self.database) as connection:
                row = connection.execute(
                    "SELECT * FROM task_journal WHERE task_id = ?",
                    (normalized_id,),
                ).fetchone()
        except sqlite3.Error:
            return None
        return _row_to_task(row) if row is not None else None

    def recent_tasks(
        self, limit: int = 20, status: str | None = None
    ) -> list[dict[str, Any]]:
        """Return recently updated tasks, newest first.

        Raises ``ValueError`` for an invalid limit or status filter.
        """

        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("limit must be an integer between 1 and 100")

        normalized_status: str | None = None
        if status is not None:
            try:
                normalized_status = _normalize_task_status(status)
            except _Rejected as exc:
                raise ValueError(exc.message) from None

        try:
            with self._lock, _sqlite_connection(self.database) as connection:
                if normalized_status is None:
                    rows = connection.execute(
                        """
                        SELECT * FROM task_journal
                        ORDER BY updated_at DESC, rowid DESC
                        LIMIT ?
                        """,
                        (limit,),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """
                        SELECT * FROM task_journal
                        WHERE status = ?
                        ORDER BY updated_at DESC, rowid DESC
                        LIMIT ?
                        """,
                        (normalized_status, limit),
                    ).fetchall()
        except sqlite3.Error:
            return []

        return [_row_to_task(row) for row in rows]

    @staticmethod
    def _failure(code: str, message: str) -> TaskJournalResult:
        return TaskJournalResult(
            ok=False, error_code=code, error=message
        )


def _row_to_task(row: sqlite3.Row) -> dict[str, Any]:
    try:
        checkpoint = json.loads(row["checkpoint_json"])
    except (TypeError, ValueError):
        checkpoint = {}
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    return {
        "task_id": row["task_id"],
        "request_label": row["request_label"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "checkpoint": checkpoint,
    }
