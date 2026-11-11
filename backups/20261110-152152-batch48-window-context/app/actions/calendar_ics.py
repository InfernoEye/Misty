"""Isolated, safety-first ICS calendar engine.

The model never supplies an absolute filesystem path. Callers pass one of the
approved folder keys understood by :class:`UserFolderResolver` plus a bounded
relative ``.ics`` path. Reads are strictly read-only. Every write is approval
gated, bounded, and performed atomically through a temporary file in the same
directory followed by :func:`os.replace`.

Timestamp model
---------------
* Timed values inside an ICS file are parsed with :mod:`zoneinfo` when a
  ``TZID`` parameter is present, from the trailing ``Z`` when UTC, and floating
  values are interpreted as UTC. Every event is reported with its original
  ISO-8601 offset in ``start``/``end`` and a normalized UTC instant in
  ``start_utc``/``end_utc`` (``...Z``).
* All-day values are calendar dates (``YYYY-MM-DD``); their UTC instants are
  reported at midnight UTC. Per RFC 5545 the all-day end date is exclusive.
* Inputs to :meth:`IcsCalendar.add_event` accept all-day dates or ISO-8601
  datetimes that carry an explicit offset. Timed writes are normalized to UTC
  (``DTSTART:...Z``); all-day writes use ``VALUE=DATE``.
* Writes always emit CRLF line endings. Adding to an existing file
  re-terminates its lines to CRLF without changing any property value.

This module deliberately contains no registry wiring, UI, model, network, or
shell imports.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.actions.file_scope import FileScopeError, UserFolderResolver


MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_EVENTS = 2000
MAX_EVENT_DURATION_DAYS = 366
MAX_SUMMARY_LENGTH = 200
MAX_TEXT_FIELD_LENGTH = 1000
DEFAULT_LIMIT = 100
MAX_RESULT_LIMIT = 500
MAX_UID_LENGTH = 512

MAX_PATH_LENGTH = 1024
MAX_PATH_BYTES = 4096
MAX_COMPONENT_BYTES = 255
MAX_DEPTH = 32
MAX_UID_ATTEMPTS = 10

CRLF = "\r\n"

_MESSAGES = {
    "invalid_folder": "The folder is not on the approved allowlist.",
    "invalid_path": (
        "The relative path must be a plain .ics file inside the approved "
        "folder."
    ),
    "file_not_found": "The calendar file does not exist.",
    "file_too_large": "The calendar file is too large.",
    "invalid_ics": "The file is not a valid ICS calendar.",
    "approval_required": "Confirmation is required. Nothing was written.",
    "duplicate_uid": "The calendar already contains duplicate event IDs.",
    "write_failed": "The calendar file could not be written.",
    "too_many_events": "The calendar contains too many events.",
    "invalid_event": "The event is not valid.",
}

_DATE_ONLY = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DATE_TIME = re.compile(
    r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?(Z)?$", re.IGNORECASE
)
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_DATETIME = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})"
    r"(?::(\d{2})(?:\.(\d{1,6}))?)?"
    r"(Z|[+-]\d{2}:?\d{2})?$",
    re.IGNORECASE,
)
_DURATION = re.compile(
    r"^(?P<sign>[+-])?P"
    r"(?:(?P<weeks>\d+)W)?"
    r"(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+)S)?)?$"
)
_PROPERTY_NAME = re.compile(r"^[A-Z0-9-]+$")


class _IcsFailure(Exception):
    """Internal parser failure carrying a fixed public code."""

    def __init__(self, code, message=None):
        self.code = code
        self.message = message or _MESSAGES.get(code, _MESSAGES["invalid_ics"])
        super().__init__(self.message)


class _InputError(Exception):
    """Internal validation failure for user supplied event fields."""


# ----------------------------------------------------------------------
# Bounded helpers
# ----------------------------------------------------------------------


def _clip(text, limit):
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _utc_iso(value, all_day):
    if all_day:
        return f"{value.isoformat()}T00:00:00Z"
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _instant_utc(value, all_day):
    if all_day:
        return datetime.combine(value, time(0), tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timezone_label(value):
    if value.utcoffset() == timedelta(0):
        return "UTC"
    key = getattr(value.tzinfo, "key", None)
    if isinstance(key, str) and key:
        return key
    offset = value.utcoffset()
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    return f"{sign}{hours:02d}:{minutes:02d}"


def _unescape_text(value):
    """Decode RFC 5545 TEXT escapes without an unbounded expansion."""
    out = []
    index = 0
    length = len(value)
    while index < length:
        char = value[index]
        if char == "\\" and index + 1 < length:
            following = value[index + 1]
            if following in "nN":
                out.append("\n")
            elif following in ",;\\":
                out.append(following)
            else:
                out.append(following)
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _escape_text(value):
    """Encode TEXT for an ICS property value."""
    out = []
    for char in value:
        if char == "\\":
            out.append("\\\\")
        elif char == ";":
            out.append("\\;")
        elif char == ",":
            out.append("\\,")
        elif char in "\r\n":
            out.append("\\n")
        else:
            out.append(char)
    return "".join(out)


def _split_head(head):
    """Split a property head on ';' outside of quoted parameter values."""
    parts = []
    current = []
    quoted = False
    for char in head:
        if char == '"':
            quoted = not quoted
            current.append(char)
        elif char == ";" and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _parse_content_line(line):
    index = None
    quoted = False
    for position, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ":" and not quoted:
            index = position
            break
    if index is None:
        raise _IcsFailure("invalid_ics")

    head, value = line[:index], line[index + 1 :]
    pieces = _split_head(head)
    name = pieces[0].strip().upper()
    if not name or _PROPERTY_NAME.fullmatch(name) is None:
        raise _IcsFailure("invalid_ics")

    params = {}
    for piece in pieces[1:]:
        key, separator, raw = piece.partition("=")
        if not separator:
            continue
        key = key.strip().upper()
        raw = raw.strip()
        if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1]
        if key:
            params[key] = raw
    return name, params, value


def _unfold(data):
    """Return decoded, unfolded content lines.

    Folding happens on octet boundaries, so lines are unfolded on bytes
    first and decoded afterwards to keep multi-byte characters intact.
    """
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    physical = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
    logical = []
    for raw in physical:
        if raw[:1] in (b" ", b"\t"):
            if not logical:
                raise _IcsFailure("invalid_ics")
            logical[-1].append(raw[1:])
        else:
            logical.append([raw])

    lines = []
    for parts in logical:
        raw = b"".join(parts)
        if not raw.strip():
            continue
        try:
            lines.append(raw.decode("utf-8"))
        except UnicodeDecodeError as error:
            raise _IcsFailure("invalid_ics") from error
    return lines


# ----------------------------------------------------------------------
# Date/time parsing
# ----------------------------------------------------------------------


def _parse_ics_moment(prop):
    """Parse DTSTART/DTEND into ``(value, all_day, timezone_label)``."""
    _name, params, raw_value = prop
    raw = raw_value.strip()
    value_type = params.get("VALUE", "").strip().upper()

    if value_type == "DATE" or _DATE_ONLY.fullmatch(raw):
        match = _DATE_ONLY.fullmatch(raw)
        if match is None:
            raise _IcsFailure("invalid_ics")
        try:
            value = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError as error:
            raise _IcsFailure("invalid_ics") from error
        return value, True, None

    if value_type not in ("", "DATE-TIME"):
        raise _IcsFailure("invalid_ics")

    match = _DATE_TIME.fullmatch(raw)
    if match is None:
        raise _IcsFailure("invalid_ics")
    try:
        value = datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4)),
            int(match.group(5)),
            int(match.group(6) or 0),
        )
    except ValueError as error:
        raise _IcsFailure("invalid_ics") from error

    tzid = params.get("TZID")
    if match.group(7):
        value = value.replace(tzinfo=timezone.utc)
        label = "UTC"
    elif tzid:
        try:
            zone = ZoneInfo(tzid)
        except Exception as error:
            raise _IcsFailure("invalid_ics") from error
        value = value.replace(tzinfo=zone)
        label = tzid
    else:
        # Floating time: interpreted deterministically as UTC.
        value = value.replace(tzinfo=timezone.utc)
        label = "UTC"
    return value, False, label


def _parse_ics_duration(raw):
    match = _DURATION.fullmatch(raw.strip().upper())
    if match is None or match.group("sign") in ("+", "-"):
        raise _IcsFailure("invalid_ics")
    groups = match.groupdict()
    units = ("weeks", "days", "hours", "minutes", "seconds")
    if not any(groups[name] for name in units):
        raise _IcsFailure("invalid_ics")
    if groups["weeks"] and (groups["days"] or groups["hours"]):
        raise _IcsFailure("invalid_ics")
    try:
        delta = timedelta(
            weeks=int(groups["weeks"] or 0),
            days=int(groups["days"] or 0),
            hours=int(groups["hours"] or 0),
            minutes=int(groups["minutes"] or 0),
            seconds=int(groups["seconds"] or 0),
        )
    except OverflowError as error:
        raise _IcsFailure("invalid_ics") from error

    if delta <= timedelta(0):
        raise _IcsFailure("invalid_ics")
    if delta > timedelta(days=MAX_EVENT_DURATION_DAYS):
        raise _IcsFailure("invalid_ics")
    return delta


def _first_property(properties, name):
    for prop in properties:
        if prop[0] == name:
            return prop
    return None


def _text_property(properties, name, limit):
    prop = _first_property(properties, name)
    text = _unescape_text(prop[2]) if prop is not None else ""
    return _clip(text, limit)


def _build_parsed_event(properties):
    start_prop = _first_property(properties, "DTSTART")
    if start_prop is None:
        raise _IcsFailure("invalid_ics")

    start, start_all_day, start_label = _parse_ics_moment(start_prop)
    end_prop = _first_property(properties, "DTEND")
    duration_prop = _first_property(properties, "DURATION")

    if end_prop is not None:
        end, end_all_day, _end_label = _parse_ics_moment(end_prop)
        if end_all_day != start_all_day:
            raise _IcsFailure("invalid_ics")
    elif duration_prop is not None:
        delta = _parse_ics_duration(duration_prop[2])
        if start_all_day:
            if delta.seconds or delta.microseconds:
                raise _IcsFailure("invalid_ics")
            end = start + timedelta(days=delta.days)
        else:
            end = start + delta
    elif start_all_day:
        end = start + timedelta(days=1)
    else:
        end = start

    if start_all_day:
        if end < start:
            raise _IcsFailure("invalid_ics")
        delta = end - start
        duration_seconds = delta.days * 86400
    else:
        start_instant = start.astimezone(timezone.utc)
        end_instant = end.astimezone(timezone.utc)
        if end_instant < start_instant:
            raise _IcsFailure("invalid_ics")
        delta = end_instant - start_instant
        duration_seconds = int(delta.total_seconds())
    if duration_seconds > MAX_EVENT_DURATION_DAYS * 86400:
        raise _IcsFailure("invalid_ics")

    uid_prop = _first_property(properties, "UID")
    uid = None
    if uid_prop is not None:
        uid = uid_prop[2].strip()
        uid = uid[:MAX_UID_LENGTH] or None

    summary, summary_clipped = _text_property(
        properties, "SUMMARY", MAX_SUMMARY_LENGTH
    )
    description, description_clipped = _text_property(
        properties, "DESCRIPTION", MAX_TEXT_FIELD_LENGTH
    )
    location, location_clipped = _text_property(
        properties, "LOCATION", MAX_TEXT_FIELD_LENGTH
    )

    return {
        "uid": uid,
        "summary": summary,
        "description": description,
        "location": location,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "start_utc": _utc_iso(start, start_all_day),
        "end_utc": _utc_iso(end, start_all_day),
        "all_day": bool(start_all_day),
        "timezone": start_label if not start_all_day else None,
        "duration_seconds": int(duration_seconds),
        "recurring": _first_property(properties, "RRULE") is not None,
        "truncated": bool(
            summary_clipped or description_clipped or location_clipped
        ),
    }


def _parse_document(data):
    """Parse one ICS document into bounded event dictionaries."""
    lines = _unfold(data)
    stack = []
    current = None
    events = []
    seen_calendar = False

    for line in lines:
        name, _params, value = _parse_content_line(line)
        if name == "BEGIN":
            component = value.strip().upper()
            if not component:
                raise _IcsFailure("invalid_ics")
            if not stack:
                if component != "VCALENDAR":
                    raise _IcsFailure("invalid_ics")
                seen_calendar = True
            stack.append(component)
            if component == "VEVENT":
                if current is not None:
                    raise _IcsFailure("invalid_ics")
                current = []
                if len(events) >= MAX_EVENTS:
                    raise _IcsFailure("too_many_events")
        elif name == "END":
            component = value.strip().upper()
            if not stack or stack[-1] != component:
                raise _IcsFailure("invalid_ics")
            stack.pop()
            if component == "VEVENT":
                if current is None:
                    raise _IcsFailure("invalid_ics")
                events.append(_build_parsed_event(current))
                current = None
        elif current is not None and stack and stack[-1] == "VEVENT":
            current.append((name, _params, value))

    if stack or current is not None or not seen_calendar:
        raise _IcsFailure("invalid_ics")
    return events


# ----------------------------------------------------------------------
# Engine
# ----------------------------------------------------------------------


class IcsCalendar:
    """Bounded ICS read/write operations inside approved user folders.

    ``resolver`` defaults to the production :class:`UserFolderResolver`.
    """

    def __init__(self, resolver=None):
        self.resolver = resolver or UserFolderResolver()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_events(
        self,
        folder,
        relative_path,
        start=None,
        end=None,
        limit=DEFAULT_LIMIT,
    ):
        """Read one bounded .ics file and return JSON-safe event dictionaries.

        ``start``/``end`` select events overlapping the requested window.
        They accept ``date``, aware ``datetime``, or ISO-8601 strings. Naive
        datetimes are treated as UTC for filtering only. An all-day event is
        included when its exclusive end is after the window start and its
        start is before the window end.
        """
        try:
            return self._list_events(
                folder, relative_path, start, end, limit
            )
        except Exception:
            return self._failure("invalid_ics")

    def add_event(
        self,
        folder,
        relative_path,
        summary,
        start,
        end=None,
        description="",
        location="",
        confirmed=False,
    ):
        """Append one event to an existing .ics or create a new calendar.

        Without ``confirmed=True`` the call validates everything, reports
        ``approval_required``, and writes nothing. Writes are atomic and only
        replace the selected .ics file after revalidating it immediately
        before the replacement.
        """
        try:
            return self._add_event(
                folder,
                relative_path,
                summary,
                start,
                end,
                description,
                location,
                confirmed,
            )
        except Exception:
            return self._failure("write_failed")

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    @staticmethod
    def _result(
        success,
        code,
        message,
        events=None,
        event=None,
        file_info=None,
        added=False,
        **extra,
    ):
        result = {
            "success": bool(success),
            "code": code,
            "message": message,
            "events": list(events or []),
            "event": event,
            "file": file_info,
            "added": bool(added),
        }
        result.update(extra)
        return result

    def _failure(self, code, file_info=None, message=None, **extra):
        return self._result(
            False,
            code,
            message or _MESSAGES.get(code, _MESSAGES["invalid_ics"]),
            file_info=file_info,
            **extra,
        )

    @staticmethod
    def _file_info(folder, relative, exists, size, count):
        return {
            "folder": folder,
            "relative_path": str(relative) if relative is not None else None,
            "exists": bool(exists),
            "size": int(size or 0),
            "event_count": count,
        }

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def _list_events(self, folder, relative_path, start, end, limit):
        info, code = self._locate(folder, relative_path, must_exist=True)
        if code:
            return self._failure(code)

        window_start, code = self._window_bound(start, "start")
        if code:
            return self._failure(
                "invalid_event",
                file_info=self._file_info(
                    folder, info["relative"], True, info["size"], None
                ),
                message=code,
            )
        window_end, code = self._window_bound(end, "end")
        if code:
            return self._failure(
                "invalid_event",
                file_info=self._file_info(
                    folder, info["relative"], True, info["size"], None
                ),
                message=code,
            )
        if (
            window_start is not None
            and window_end is not None
            and window_start >= window_end
        ):
            return self._failure(
                "invalid_event",
                file_info=self._file_info(
                    folder, info["relative"], True, info["size"], None
                ),
                message="The range start must be before the range end.",
            )

        limit = self._clamp_limit(limit)
        data, code = self._read_bytes(info)
        if code:
            return self._failure(
                code,
                file_info=self._file_info(
                    folder, info["relative"], True, info["size"], None
                ),
            )

        try:
            events = _parse_document(data)
        except _IcsFailure as failure:
            return self._failure(
                failure.code,
                file_info=self._file_info(
                    folder, info["relative"], True, info["size"], None
                ),
                message=failure.message,
            )

        selected = []
        matched = 0
        for event in events:
            if not self._in_window(event, window_start, window_end):
                continue
            matched += 1
            if len(selected) < limit:
                selected.append(event)

        return self._result(
            True,
            None,
            "Calendar read.",
            events=selected,
            file_info=self._file_info(
                folder, info["relative"], True, info["size"], len(events)
            ),
            count=len(selected),
            total=matched,
            truncated=matched > len(selected),
            limit=limit,
        )

    def _read_bytes(self, info):
        target = info["target"]
        if info["size"] > MAX_FILE_BYTES:
            return None, "file_too_large"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags)
        except FileNotFoundError:
            return None, "file_not_found"
        except OSError:
            return None, "invalid_path"
        try:
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = None
                data = handle.read(MAX_FILE_BYTES + 1)
        except OSError:
            return None, "invalid_path"
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if len(data) > MAX_FILE_BYTES:
            return None, "file_too_large"
        return data, None

    @staticmethod
    def _clamp_limit(limit):
        try:
            value = int(limit)
        except (TypeError, ValueError):
            return DEFAULT_LIMIT
        return max(1, min(value, MAX_RESULT_LIMIT))

    @staticmethod
    def _window_bound(value, label):
        if value is None:
            return None, None
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc), None
        if isinstance(value, date):
            return datetime.combine(value, time(0), tzinfo=timezone.utc), None
        if not isinstance(value, str):
            return None, f"The range {label} must be an ISO-8601 value."

        text = value.strip()
        if not text:
            return None, f"The range {label} must not be empty."
        if _ISO_DATE.fullmatch(text):
            try:
                day = date.fromisoformat(text)
            except ValueError:
                return None, f"The range {label} is not a valid date."
            return datetime.combine(day, time(0), tzinfo=timezone.utc), None
        match = _ISO_DATETIME.fullmatch(text)
        if match is None:
            return None, f"The range {label} is not a valid ISO-8601 value."
        try:
            parsed = datetime.fromisoformat(
                text.replace("Z", "+00:00").replace("z", "+00:00")
            )
        except ValueError:
            return None, f"The range {label} is not a valid ISO-8601 value."
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc), None

    @staticmethod
    def _in_window(event, window_start, window_end):
        start = datetime.strptime(
            event["start_utc"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        end = datetime.strptime(
            event["end_utc"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        if end == start:
            if window_start is not None and start < window_start:
                return False
            if window_end is not None and start >= window_end:
                return False
            return True
        if window_start is not None and end <= window_start:
            return False
        if window_end is not None and start >= window_end:
            return False
        return True

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def _add_event(
        self,
        folder,
        relative_path,
        summary,
        start,
        end,
        description,
        location,
        confirmed,
    ):
        info, code = self._locate(folder, relative_path, must_exist=False)
        if code:
            return self._failure(code)

        try:
            spec = self._build_spec(
                summary, start, end, description, location
            )
        except _InputError as error:
            return self._failure(
                "invalid_event",
                file_info=self._file_info(
                    folder, info["relative"], info["exists"], info["size"], None
                ),
                message=str(error),
            )

        existing_data = None
        existing_events = []
        if info["exists"]:
            existing_data, code = self._read_bytes(info)
            if code:
                return self._failure(
                    code,
                    file_info=self._file_info(
                        folder, info["relative"], True, info["size"], None
                    ),
                )
            try:
                existing_events = _parse_document(existing_data)
            except _IcsFailure as failure:
                return self._failure(
                    failure.code,
                    file_info=self._file_info(
                        folder, info["relative"], True, info["size"], None
                    ),
                    message=failure.message,
                )
            uids = [event["uid"] for event in existing_events if event["uid"]]
            if len(uids) != len(set(uids)):
                return self._failure(
                    "duplicate_uid",
                    file_info=self._file_info(
                        folder,
                        info["relative"],
                        True,
                        info["size"],
                        len(existing_events),
                    ),
                )
            if len(existing_events) >= MAX_EVENTS:
                return self._failure(
                    "too_many_events",
                    file_info=self._file_info(
                        folder,
                        info["relative"],
                        True,
                        info["size"],
                        len(existing_events),
                    ),
                )

        uid = self._new_uid(existing_events)
        new_event = self._event_payload(spec, uid)
        file_info = self._file_info(
            folder,
            info["relative"],
            info["exists"],
            info["size"],
            len(existing_events) if info["exists"] else 0,
        )

        if not confirmed:
            return self._failure(
                "approval_required",
                file_info=file_info,
                event=new_event,
                requires_confirmation=True,
            )

        block = self._render_event(spec, uid)
        if existing_data is None:
            payload = self._new_document(block)
        else:
            try:
                payload = self._insert_event(existing_data, block)
            except _IcsFailure as failure:
                return self._failure(
                    failure.code,
                    file_info=file_info,
                    message=failure.message,
                )

        if len(payload) > MAX_FILE_BYTES:
            return self._failure("file_too_large", file_info=file_info)

        code = self._atomic_write(info, payload)
        if code:
            return self._failure(code, file_info=file_info)

        new_info = self._file_info(
            folder,
            info["relative"],
            True,
            len(payload),
            len(existing_events) + 1,
        )
        return self._result(
            True,
            None,
            "Event added to the calendar.",
            file_info=new_info,
            event=new_event,
            added=True,
        )

    def _build_spec(self, summary, start, end, description, location):
        if not isinstance(summary, str):
            raise _InputError("The summary must be text.")
        if not summary.strip():
            raise _InputError("The summary must not be empty.")
        if len(summary) > MAX_SUMMARY_LENGTH:
            raise _InputError("The summary is too long.")
        if "\x00" in summary:
            raise _InputError("The summary contains an invalid character.")

        if description is None:
            description = ""
        if location is None:
            location = ""
        if not isinstance(description, str):
            raise _InputError("The description must be text.")
        if not isinstance(location, str):
            raise _InputError("The location must be text.")
        if len(description) > MAX_TEXT_FIELD_LENGTH:
            raise _InputError("The description is too long.")
        if len(location) > MAX_TEXT_FIELD_LENGTH:
            raise _InputError("The location is too long.")
        if "\x00" in description or "\x00" in location:
            raise _InputError("A text field contains an invalid character.")

        start_kind, start_value = self._input_moment(start)
        if end is None:
            if start_kind == "date":
                end_value = start_value + timedelta(days=1)
            else:
                end_value = start_value + timedelta(hours=1)
        else:
            end_kind, end_value = self._input_moment(end)
            if end_kind != start_kind:
                raise _InputError(
                    "The start and end must both be all-day or both timed."
                )

        if end_value <= start_value:
            raise _InputError("The end must be after the start.")
        duration = end_value - start_value
        if duration > timedelta(days=MAX_EVENT_DURATION_DAYS):
            raise _InputError("The event is longer than 366 days.")

        return {
            "summary": summary,
            "description": description,
            "location": location,
            "start": start_value,
            "end": end_value,
            "all_day": start_kind == "date",
            "timezone": (
                None
                if start_kind == "date"
                else _timezone_label(start_value)
            ),
            "start_utc": _utc_iso(start_value, start_kind == "date"),
            "end_utc": _utc_iso(end_value, start_kind == "date"),
            "duration_seconds": int(duration.total_seconds()),
        }

    @staticmethod
    def _input_moment(value):
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise _InputError("Timed values must include a timezone.")
            return "datetime", value
        if isinstance(value, date):
            return "date", value
        if not isinstance(value, str):
            raise _InputError(
                "Dates must be ISO-8601 strings, dates, or datetimes."
            )

        text = value.strip()
        if not text:
            raise _InputError("The date must not be empty.")
        if _ISO_DATE.fullmatch(text):
            try:
                return "date", date.fromisoformat(text)
            except ValueError as error:
                raise _InputError("The date is not a valid calendar date.") from error

        match = _ISO_DATETIME.fullmatch(text)
        if match is None:
            raise _InputError("The date is not a valid ISO-8601 value.")
        if not match.group(8):
            raise _InputError("Timed values must include a timezone.")
        try:
            parsed = datetime.fromisoformat(
                text.replace("Z", "+00:00").replace("z", "+00:00")
            )
        except ValueError as error:
            raise _InputError("The date is not a valid ISO-8601 value.") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise _InputError("Timed values must include a timezone.")
        return "datetime", parsed

    @staticmethod
    def _event_payload(spec, uid):
        return {
            "uid": uid,
            "summary": spec["summary"],
            "description": spec["description"],
            "location": spec["location"],
            "start": spec["start"].isoformat(),
            "end": spec["end"].isoformat(),
            "start_utc": spec["start_utc"],
            "end_utc": spec["end_utc"],
            "all_day": bool(spec["all_day"]),
            "timezone": spec["timezone"],
            "duration_seconds": int(spec["duration_seconds"]),
            "recurring": False,
            "truncated": False,
        }

    @staticmethod
    def _new_uid(existing_events):
        taken = {event["uid"] for event in existing_events if event["uid"]}
        for _ in range(MAX_UID_ATTEMPTS):
            candidate = f"{uuid.uuid4().hex}@misty.local"
            if candidate not in taken:
                return candidate
        return f"{uuid.uuid4().hex}{uuid.uuid4().hex}@misty.local"

    @staticmethod
    def _render_event(spec, uid):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lines = [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}",
            f"SUMMARY:{_escape_text(spec['summary'])}",
        ]
        if spec["all_day"]:
            lines.append(f"DTSTART;VALUE=DATE:{spec['start'].strftime('%Y%m%d')}")
            lines.append(f"DTEND;VALUE=DATE:{spec['end'].strftime('%Y%m%d')}")
        else:
            start = spec["start"].astimezone(timezone.utc)
            end = spec["end"].astimezone(timezone.utc)
            lines.append(f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}")
            lines.append(f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}")
        if spec["description"]:
            lines.append(f"DESCRIPTION:{_escape_text(spec['description'])}")
        if spec["location"]:
            lines.append(f"LOCATION:{_escape_text(spec['location'])}")
        lines.append("END:VEVENT")
        return CRLF.join(lines) + CRLF

    @staticmethod
    def _new_document(block):
        text = (
            "BEGIN:VCALENDAR" + CRLF
            + "VERSION:2.0" + CRLF
            + "PRODID:-//Misty//ICS Calendar//EN" + CRLF
            + "CALSCALE:GREGORIAN" + CRLF
            + block
            + "END:VCALENDAR" + CRLF
        )
        return text.encode("utf-8")

    @staticmethod
    def _insert_event(data, block):
        """Insert a block before the closing END:VCALENDAR.

        Every line terminator is normalized to CRLF; all property values and
        components are preserved verbatim.
        """
        normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        lines = normalized.split(b"\n")
        index = None
        for position, line in enumerate(lines):
            if line.rstrip().upper() == b"END:VCALENDAR":
                index = position
        if index is None:
            raise _IcsFailure("invalid_ics")

        prefix = b"\r\n".join(lines[:index])
        if prefix:
            prefix += CRLF.encode("ascii")
        suffix = b"\r\n".join(lines[index:])
        if not suffix.endswith(b"\n"):
            suffix += CRLF.encode("ascii")
        return prefix + block.encode("utf-8") + suffix

    @staticmethod
    def _write_path_safe(info):
        """Recheck parents and target without following any symlink."""
        base = info["base"]
        parent = base
        for part in Path(info["relative"]).parts[:-1]:
            parent = parent / part
            try:
                st = os.lstat(parent)
            except OSError:
                return False
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                return False
        try:
            if not parent.resolve(strict=True).is_relative_to(base):
                return False
        except (OSError, RuntimeError):
            return False

        try:
            st = os.lstat(info["target"])
        except FileNotFoundError:
            st = None
        except OSError:
            return False

        if info["exists"]:
            if st is None:
                return False
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                return False
        elif st is not None:
            # Never clobber a file that appeared after the read.
            return False
        return True

    def _atomic_write(self, info, data):
        target = info["target"]
        parent = target.parent
        descriptor = None
        temp_path = None
        try:
            descriptor, temp_name = tempfile.mkstemp(
                prefix=".misty-ics-", suffix=".tmp", dir=parent
            )
            temp_path = Path(temp_name)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())

            if temp_path.read_bytes() != data:
                return "write_failed"

            # Revalidate the whole path immediately before the replacement.
            if not self._write_path_safe(info):
                return "write_failed"

            os.replace(temp_path, target)
            temp_path = None
            self._fsync_directory(parent)
            return None
        except OSError:
            return "write_failed"
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temp_path is not None:
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except OSError:
                    pass

    @staticmethod
    def _fsync_directory(directory):
        try:
            descriptor = os.open(
                directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Scope resolution
    # ------------------------------------------------------------------

    def _base(self, folder):
        try:
            if not isinstance(folder, str) or not folder:
                return None, "invalid_folder"
            base = Path(self.resolver.base_for_key(folder))
            if not base.is_dir() or base.is_symlink():
                return None, "invalid_folder"
            return base.resolve(strict=True), None
        except (
            FileScopeError,
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
        ):
            return None, "invalid_folder"

    @staticmethod
    def _relative(relative_path):
        if not isinstance(relative_path, str):
            return None, "invalid_path"
        if not relative_path or relative_path.endswith("/"):
            return None, "invalid_path"
        if len(relative_path) > MAX_PATH_LENGTH or "\x00" in relative_path:
            return None, "invalid_path"
        try:
            if len(os.fsencode(relative_path)) > MAX_PATH_BYTES:
                return None, "invalid_path"
            path = Path(relative_path)
        except (OSError, RuntimeError, ValueError, TypeError):
            return None, "invalid_path"

        if path.is_absolute() or not path.parts:
            return None, "invalid_path"
        if ".." in path.parts:
            return None, "invalid_path"
        if len(path.parts) > MAX_DEPTH:
            return None, "invalid_path"
        try:
            if any(
                len(os.fsencode(part)) > MAX_COMPONENT_BYTES
                for part in path.parts
            ):
                return None, "invalid_path"
        except (OSError, RuntimeError, ValueError, TypeError):
            return None, "invalid_path"
        if path.suffix.lower() != ".ics":
            return None, "invalid_path"
        return path, None

    def _locate(self, folder, relative_path, must_exist):
        base, code = self._base(folder)
        if code:
            return None, code
        relative, code = self._relative(relative_path)
        if code:
            return None, code

        parent = base
        for part in relative.parts[:-1]:
            parent = parent / part
            try:
                st = os.lstat(parent)
            except (FileNotFoundError, NotADirectoryError):
                return None, "invalid_path"
            except OSError:
                return None, "invalid_path"
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                return None, "invalid_path"

        target = base / relative
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            st = None
        except OSError:
            return None, "invalid_path"

        if st is not None:
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                return None, "invalid_path"
        elif must_exist:
            return None, "file_not_found"

        try:
            resolved_parent = parent.resolve(strict=True)
            if not resolved_parent.is_relative_to(base):
                return None, "invalid_path"
        except (OSError, RuntimeError):
            return None, "invalid_path"

        return (
            {
                "base": base,
                "relative": str(relative),
                "target": target,
                "exists": st is not None,
                "size": int(st.st_size) if st is not None else 0,
            },
            None,
        )
