"""Request-bound confirmation state for consequential actions.

A confirmation is valid only for the request that produced it and only until
its deadline. This module deliberately has no Qt, model, or desktop imports so
its policy can be tested in isolation.
"""

from __future__ import annotations

import threading
import time
import uuid


DEFAULT_CONFIRMATION_TIMEOUT_SECONDS = 60.0


class ConfirmationBroker:
    """Own at most one pending confirmation at a time."""

    def __init__(self, timeout=DEFAULT_CONFIRMATION_TIMEOUT_SECONDS, clock=None):
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("Confirmation timeout must be positive.")
        self.timeout = float(timeout)
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._pending = None

    def begin(self, name, arguments, description):
        """Create a pending request, or return None if one already exists."""
        pending = {
            "request_id": uuid.uuid4().hex,
            "name": name,
            "arguments": dict(arguments),
            "description": description,
            "event": threading.Event(),
            "approved": False,
            "expires_at": self._clock() + self.timeout,
        }

        with self._lock:
            if self._pending is not None:
                return None
            self._pending = pending
        return pending

    def is_pending(self):
        with self._lock:
            return self._pending is not None

    def current_request_id(self):
        with self._lock:
            return (
                self._pending["request_id"]
                if self._pending is not None else None
            )

    def wait(self, pending):
        """Wait for a decision; returns True only for an unexpired approval."""
        pending["event"].wait(self.timeout)

        with self._lock:
            if self._pending is pending:
                self._pending = None
            expired = self._clock() >= pending["expires_at"]
            return bool(pending["approved"]) and not expired

    @staticmethod
    def _is_valid_request_id(request_id):
        return isinstance(request_id, str) and bool(request_id.strip())

    def resolve(self, approved, request_id):
        """Resolve the pending request only for an exact request ID match.

        Returns True when this call resolved the request. A missing or empty
        request ID, a mismatched request ID, an expired request, or no pending
        request returns False and leaves any pending request untouched (except
        for expiry, which clears it).
        """
        if not self._is_valid_request_id(request_id):
            return False

        with self._lock:
            pending = self._pending
            if pending is None or pending["event"].is_set():
                return False

            if request_id != pending["request_id"]:
                return False

            return self._resolve_locked(approved, pending)

    def _resolve_locked(self, approved, pending):
        if self._clock() >= pending["expires_at"]:
            pending["approved"] = False
            pending["event"].set()
            if self._pending is pending:
                self._pending = None
            return False

        pending["approved"] = bool(approved)
        pending["event"].set()
        if self._pending is pending:
            self._pending = None
        return True

    def cancel(self):
        """Deny and clear any pending request without executing anything."""
        with self._lock:
            pending = self._pending
            if pending is None or pending["event"].is_set():
                return False
            return self._resolve_locked(False, pending)
