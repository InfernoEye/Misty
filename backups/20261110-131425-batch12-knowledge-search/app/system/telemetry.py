"""Isolated, bounded, read-only system telemetry for Misty.

The module exposes one stateless service, :class:`SystemTelemetry`, which
wraps a small, fixed subset of :mod:`psutil`:

* :meth:`SystemTelemetry.summary` returns CPU, load, memory, swap, uptime,
  process count, boot time, and root-filesystem usage.
* :meth:`SystemTelemetry.top_processes` returns a bounded list of processes
  with names and percentages only.

Privacy and safety decisions
----------------------------
* No command lines, environment variables, usernames, executable paths, open
  files, connections, or arbitrary filesystem paths are read or returned.
* The only path accepted by psutil is the fixed root path ``"/"``.
* Nothing is written, launched, cached, logged, or sent over a network.
* All values are clamped, rounded, and plain JSON-safe Python objects.
* psutil and OS errors are translated into ``{"success": False, ...}`` with a
  fixed message; callers never receive a psutil exception.

The service keeps no mutable state.  A module-level lock serialises snapshots
so concurrent callers cannot interleave the non-blocking CPU counter reads;
this is synchronisation only, not caching.
"""

from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone

import psutil

__all__ = ["MAX_PROCESS_LIMIT", "SystemTelemetry"]

#: Largest accepted process limit.
MAX_PROCESS_LIMIT = 20

#: Default process count for :meth:`SystemTelemetry.top_processes`.
DEFAULT_PROCESS_LIMIT = 5

#: Longest process name returned to callers (characters).
MAX_PROCESS_NAME_LENGTH = 128

#: The only filesystem path this module asks psutil about.
ROOT_PATH = "/"

_SORT_KEYS = ("memory", "cpu")
_PROCESS_ATTRS = ["pid", "name", "memory_percent", "cpu_percent"]

_MAX_INT = 2**63 - 1
_MAX_CPU_COUNT = 4096
_MAX_LOAD = 1_000_000.0

_MESSAGE_SUMMARY = "System telemetry is unavailable."
_MESSAGE_DISK = "Disk usage is unavailable."
_MESSAGE_LOAD = "Load average is unavailable."
_MESSAGE_PROCESSES = "Process information is unavailable."
_MESSAGE_LIMIT = "Process limit must be an integer between 1 and 20."
_MESSAGE_SORT = "Sort must be 'memory' or 'cpu'."

_SNAPSHOT_LOCK = threading.Lock()


class _TelemetryError(Exception):
    """Internal error carrying a fixed, caller-safe message."""


def _failure(message: str) -> dict:
    """Build the stable failure shape used by both public methods."""
    return {"success": False, "message": message}


def _bounded_int(value, low: int = 0, high: int = _MAX_INT) -> int:
    """Coerce *value* to a finite integer inside ``[low, high]``."""
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return low
    return max(low, min(number, high))


def _bounded_float(
    value,
    low: float = 0.0,
    high: float = 100.0,
    digits: int = 1,
) -> float:
    """Coerce *value* to a finite rounded float inside ``[low, high]``."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return round(low, digits)
    if not math.isfinite(number):
        return round(low, digits)
    return round(min(max(number, low), high), digits)


def _percent(value) -> float:
    return _bounded_float(value, 0.0, 100.0, 1)


def _bytes(value) -> int:
    return _bounded_int(value, 0, _MAX_INT)


def _sanitize_name(value) -> str:
    """Return a printable, whitespace-normalised, length-bounded name."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    elif not isinstance(value, str):
        if value is None:
            return "unknown"
        try:
            value = str(value)
        except Exception:
            return "unknown"

    text = "".join(
        character if character.isprintable() else " "
        for character in value
    )
    text = " ".join(text.split())
    if not text:
        return "unknown"
    return text[:MAX_PROCESS_NAME_LENGTH]


def _utc_iso(timestamp: float) -> str:
    """Convert a UNIX timestamp to an aware UTC ISO-8601 string."""
    try:
        moment = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError) as error:
        raise _TelemetryError(_MESSAGE_SUMMARY) from error
    return moment.isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SystemTelemetry:
    """Read-only system snapshots with fixed, JSON-safe result dictionaries.

    The class is stateless; ``SystemTelemetry()`` is cheap and one instance
    may be shared between threads.
    """

    __slots__ = ()

    def summary(self) -> dict:
        """Return one bounded telemetry snapshot.

        The CPU percentage is a non-blocking sample (``interval=None``), so
        the first call after import may report ``0.0`` or a value measured
        since the previous sample.  No psutil exception escapes this method.
        """
        with _SNAPSHOT_LOCK:
            try:
                return self._build_summary()
            except _TelemetryError as error:
                return _failure(str(error))
            except Exception:
                return _failure(_MESSAGE_SUMMARY)

    def top_processes(self, limit: int = DEFAULT_PROCESS_LIMIT,
                      sort: str = "memory") -> dict:
        """Return up to *limit* processes, sorted by ``memory`` or ``cpu``.

        Only ``pid``, ``name``, ``memory_percent``, and ``cpu_percent`` are
        requested.  Command lines are never read.  ``cpu_percent`` is a
        psutil snapshot value: it is not measured over a sleep interval and
        can be ``0.0`` for processes that have not run since the previous
        sample.  Sort by CPU only with that limitation in mind.
        """
        if isinstance(limit, bool) or not isinstance(limit, int):
            return _failure(_MESSAGE_LIMIT)
        if not 1 <= limit <= MAX_PROCESS_LIMIT:
            return _failure(_MESSAGE_LIMIT)
        if sort not in _SORT_KEYS:
            return _failure(_MESSAGE_SORT)

        with _SNAPSHOT_LOCK:
            try:
                return self._build_processes(limit, sort)
            except _TelemetryError as error:
                return _failure(str(error))
            except Exception:
                return _failure(_MESSAGE_PROCESSES)

    # -- internal builders (called while the snapshot lock is held) --------

    def _build_summary(self) -> dict:
        timestamp = _now_iso()

        cpu_percent = _percent(psutil.cpu_percent(interval=None))
        cpu_count = _bounded_int(
            psutil.cpu_count(logical=True), 0, _MAX_CPU_COUNT
        )

        try:
            load_values = tuple(psutil.getloadavg())
        except (OSError, AttributeError, NotImplementedError) as error:
            raise _TelemetryError(_MESSAGE_LOAD) from error
        if len(load_values) != 3:
            raise _TelemetryError(_MESSAGE_LOAD)
        load_average = [
            _bounded_float(value, 0.0, _MAX_LOAD, 2)
            for value in load_values
        ]

        memory_raw = psutil.virtual_memory()
        memory_total = _bytes(memory_raw.total)
        memory_available = min(_bytes(memory_raw.available), memory_total)
        memory_used = min(_bytes(memory_raw.used), memory_total)
        memory = {
            "total": memory_total,
            "available": memory_available,
            "used": memory_used,
            "percent": _percent(memory_raw.percent),
        }

        swap_raw = psutil.swap_memory()
        swap_total = _bytes(swap_raw.total)
        swap = {
            "total": swap_total,
            "used": min(_bytes(swap_raw.used), swap_total),
            "percent": _percent(swap_raw.percent),
        }

        boot_timestamp = psutil.boot_time()
        boot_time = _utc_iso(boot_timestamp)
        try:
            uptime = max(0.0, time.time() - float(boot_timestamp))
        except (TypeError, ValueError, OverflowError):
            uptime = 0.0
        uptime_seconds = _bounded_int(round(uptime), 0, _MAX_INT)

        try:
            process_count = _bounded_int(len(psutil.pids()), 0, _MAX_INT)
        except (psutil.Error, OSError) as error:
            raise _TelemetryError(_MESSAGE_SUMMARY) from error

        try:
            disk_raw = psutil.disk_usage(ROOT_PATH)
        except (psutil.Error, OSError) as error:
            raise _TelemetryError(_MESSAGE_DISK) from error

        disk_total = _bytes(disk_raw.total)
        disk = {
            "path": ROOT_PATH,
            "total": disk_total,
            "used": min(_bytes(disk_raw.used), disk_total),
            "free": min(_bytes(disk_raw.free), disk_total),
            "percent": _percent(disk_raw.percent),
        }

        return {
            "success": True,
            "message": "System telemetry retrieved.",
            "timestamp": timestamp,
            "cpu_percent": cpu_percent,
            "cpu_count": cpu_count,
            "load_average": load_average,
            "memory": memory,
            "swap": swap,
            "uptime_seconds": uptime_seconds,
            "boot_time": boot_time,
            "process_count": process_count,
            "disk": disk,
        }

    def _build_processes(self, limit: int, sort: str) -> dict:
        try:
            iterator = iter(psutil.process_iter(attrs=list(_PROCESS_ATTRS)))
        except (psutil.NoSuchProcess, psutil.AccessDenied) as error:
            raise _TelemetryError(_MESSAGE_PROCESSES) from error
        except psutil.Error as error:
            raise _TelemetryError(_MESSAGE_PROCESSES) from error

        entries = []
        while True:
            try:
                process = next(iterator)
            except StopIteration:
                break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except psutil.Error:
                raise _TelemetryError(_MESSAGE_PROCESSES)

            try:
                info = process.info
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except psutil.Error:
                continue
            except Exception:
                continue

            if not isinstance(info, dict):
                continue

            pid = info.get("pid")
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                continue

            entries.append(
                {
                    "pid": _bounded_int(pid, 1, _MAX_INT),
                    "name": _sanitize_name(info.get("name")),
                    "memory_percent": _percent(info.get("memory_percent")),
                    # Snapshot value only; see the method docstring.
                    "cpu_percent": _percent(info.get("cpu_percent")),
                }
            )

        sort_key = "memory_percent" if sort == "memory" else "cpu_percent"
        entries.sort(
            key=lambda entry: (
                -entry[sort_key],
                entry["pid"],
                entry["name"],
            )
        )
        selected = entries[:limit]

        return {
            "success": True,
            "message": "Process information retrieved.",
            "sort": sort,
            "limit": limit,
            "count": len(selected),
            "processes": selected,
        }
