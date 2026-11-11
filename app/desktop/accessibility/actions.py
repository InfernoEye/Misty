"""Isolated semantic activation adapter for accessible controls.

This module composes the existing read-only :class:`AccessibilityInspector`
with the existing :class:`AccessibilityService` action method.  It adds a
narrow, confirmation-gated "press" operation:

* matching is semantic (case-insensitive exact first, then bounded
  substring), with an optional role filter;
* only a *unique* match may be activated -- ambiguity is reported, never
  guessed;
* the service's ``perform_action`` is called with ``action_name="press"``
  and an ``expected_name`` guard so a stale positional path cannot press a
  different control;
* the control is re-inspected afterwards and the result carries a
  tri-state ``verified`` value.  A press that is accepted but cannot be
  re-identified is still a successful press, but it never claims that the
  UI changed.

The adapter is deliberately isolated: no registry wiring, no Qt/model/
network/shell imports, no logging, and no exception text or tree content in
error messages.
"""

from __future__ import annotations

import math
from typing import Any

from app.desktop.accessibility import activator as live_activator
from app.desktop.accessibility.inspector import AccessibilityInspector
from app.desktop.accessibility.service import AccessibilityService


MIN_DEPTH = 1
MAX_DEPTH = 20
MIN_NODES = 1
MAX_NODES = 5000
MAX_PROCESS_ID = 2_147_483_647
MAX_NAME_LENGTH = 256
MAX_ROLE_LENGTH = 128
MAX_CANDIDATES = 20
MAX_STATES = 32
MAX_ACTIONS = 32
MAX_PATH_LENGTH = 256
MIN_TIMEOUT_SECONDS = 0.1
MAX_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_DEPTH = 8
DEFAULT_MAX_NODES = 500
PRESS_ACTION = "press"
SUCCESS_CODE = "ok"

# The states the underlying service requires before it will press a control.
REQUIRED_STATES = frozenset({"enabled", "sensitive", "visible"})

FAILURE_CODES = frozenset(
    {
        "invalid_input",
        "approval_required",
        "not_found",
        "ambiguous",
        "action_failed",
        "stale_target",
        "verification_failed",
        "unsupported",
    }
)

_MESSAGES = {
    "invalid_input": "The activation request was invalid; nothing was performed.",
    "approval_required": (
        "Explicit user confirmation is required; nothing was performed."
    ),
    "not_found": (
        "No accessible control matched the request within the inspection bounds."
    ),
    "ambiguous": (
        "Multiple accessible controls matched; nothing was performed."
    ),
    "action_failed": "The accessibility service did not perform the action.",
    "stale_target": (
        "The control no longer matches the expected name; nothing was performed."
    ),
    "verification_failed": (
        "The press was accepted, but post-action verification failed."
    ),
    "unsupported": (
        "The matched control does not expose a supported press action; "
        "nothing was performed."
    ),
    "ok": "A unique accessible control matched.",
}

_VALIDATION_MESSAGES = {
    "process_id": (
        f"process_id must be an integer between 1 and {MAX_PROCESS_ID}."
    ),
    "name": (
        f"name must be text of 1 to {MAX_NAME_LENGTH} characters without "
        "NUL characters."
    ),
    "role": (
        f"role must be text of 1 to {MAX_ROLE_LENGTH} characters without "
        "NUL characters."
    ),
    "expected_name": (
        f"expected_name must be text of 1 to {MAX_NAME_LENGTH} characters "
        "without NUL characters."
    ),
    "max_depth": (
        f"max_depth must be an integer between {MIN_DEPTH} and {MAX_DEPTH}."
    ),
    "max_nodes": (
        f"max_nodes must be an integer between {MIN_NODES} and {MAX_NODES}."
    ),
    "confirmed": "confirmed must be a boolean.",
}


def _valid_bounded_int(value: Any, minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= maximum
    )


def _valid_process_id(value: Any) -> bool:
    return _valid_bounded_int(value, 1, MAX_PROCESS_ID)


def _normalize_text(value: Any, limit: int) -> str | None:
    """Return stripped text within bounds, or None when invalid."""
    if not isinstance(value, str):
        return None
    if len(value) > limit or "\x00" in value:
        return None
    stripped = value.strip()
    return stripped or None


_DEPRECATED_MESSAGES = {
    "unsupported": "The accessibility service is unavailable.",
}


def _legacy_target(control: dict) -> dict:
    """Legacy target shape from one live-adapter control mapping."""
    return {
        "name": str(control.get("name", ""))[:MAX_NAME_LENGTH],
        "role": str(control.get("role", ""))[:MAX_ROLE_LENGTH],
        "path": str(control.get("path", ""))[:MAX_PATH_LENGTH],
        "states": list(control.get("states") or [])[:MAX_STATES],
        # The live adapter only ever returns a unique match.
        "available": True,
    }


def _legacy_find_result(result: dict) -> dict:
    """Translate one live-adapter find result into the legacy shape."""
    control = result.get("control") if isinstance(result, dict) else None
    if isinstance(control, dict):
        target = _legacy_target(control)
        return {
            "success": True,
            "code": SUCCESS_CODE,
            "message": _MESSAGES["ok"],
            "target": target,
            "matches": [target],
            "candidates": [target],
            "count": 1,
            "truncated": False,
        }
    code = str(result.get("code") or "not_found")
    if code not in FAILURE_CODES:
        code = "action_failed"
    count = result.get("count", 0)
    return {
        "success": False,
        "code": code,
        "message": _MESSAGES.get(code, _MESSAGES["action_failed"]),
        "candidates": list(result.get("candidates") or [])[:MAX_CANDIDATES],
        "count": count if isinstance(count, int) and count >= 0 else 0,
        "truncated": False,
    }


class ControlActivator:
    """DEPRECATED: find one accessible control and, with approval, press it.

    No production code constructs this class. The live adapter is
    :class:`app.desktop.accessibility.activator.ControlActivator`, which the
    tool registry receives as ``control_activator`` and which resolves the
    application by name or PID, matches case-insensitively, and guards the
    press with the canonical label it resolved.

    For compatibility this class now delegates ``find`` and ``activate`` to
    a live adapter built from the same inspector/service pair. It never
    resolves a target itself, so the two adapters cannot drift apart again.
    New code must construct the live adapter directly.
    """

    DEPRECATED = True

    def __init__(
        self,
        inspector: Any | None = None,
        service: Any | None = None,
        timeout: float | None = None,
    ):
        if timeout is not None:
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(float(timeout))
                or not MIN_TIMEOUT_SECONDS <= float(timeout) <= MAX_TIMEOUT_SECONDS
            ):
                raise ValueError(
                    "timeout must be a number between "
                    f"{MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS}."
                )
            timeout = float(timeout)

        if inspector is None and service is None:
            self._service = AccessibilityService()
            self._inspector = AccessibilityInspector(self._service)
        else:
            self._inspector = (
                AccessibilityInspector(service)
                if inspector is None
                else inspector
            )
            self._service = service
            if self._service is None:
                # Reuse the service already wrapped by the inspector when
                # possible; otherwise activation stays unavailable rather
                # than silently constructing a live AT-SPI service.
                self._service = getattr(self._inspector, "_service", None)

        # Advisory only: the service owns its own blocking behaviour, so the
        # adapter starts no threads and never pretends to enforce this.
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Public API

    def _live(self):
        """The live adapter that actually resolves and presses controls."""
        return live_activator.ControlActivator(service=self._service)

    def _unsupported(self) -> dict:
        return {
            "success": False,
            "code": "unsupported",
            "message": _DEPRECATED_MESSAGES["unsupported"],
            "candidates": [],
            "count": 0,
            "truncated": False,
        }

    def find(
        self,
        process_id: int,
        name: str,
        role: str | None = None,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_nodes: int = DEFAULT_MAX_NODES,
    ) -> dict:
        """Find a unique control by name (and optional role), read-only.

        Delegates to the live adapter and translates its ``control`` result
        into this class's legacy ``target`` shape. ``max_depth`` and
        ``max_nodes`` are accepted for compatibility; the live adapter
        owns its own bounded traversal.
        """
        del max_depth, max_nodes
        if self._service is None:
            return self._unsupported()
        return _legacy_find_result(
            self._live().find(process_id=process_id, name=name, role=role)
        )

    def activate(
        self,
        process_id: int,
        name: str,
        role: str | None = None,
        confirmed: bool = False,
        expected_name: str | None = None,
    ) -> dict:
        """Press a unique control, but only after explicit confirmation.

        ``expected_name`` is compared against the label the live adapter
        resolved, never against the query, so case-insensitive and substring
        lookups keep working while a renamed control is still refused.
        """
        if self._service is None:
            return self._unsupported()
        live = self._live()
        found = live.find(process_id=process_id, name=name, role=role)
        if found.get("success") is not True:
            return _legacy_find_result(found)
        if (
            isinstance(expected_name, str)
            and found["control"]["name"] != expected_name
        ):
            return self._activation_failure(
                "stale_target", target=_legacy_target(found["control"])
            )
        result = live.activate(
            process_id=process_id,
            name=name,
            role=role,
            confirmed=confirmed,
        )
        if result.get("success") is not True:
            return self._activation_failure(
                result.get("code", "action_failed"),
                target=_legacy_target(result["control"])
                if isinstance(result.get("control"), dict)
                else None,
            )
        return {
            "success": True,
            "code": SUCCESS_CODE,
            "message": "Control pressed.",
            "target": _legacy_target(result["control"]),
            "action": PRESS_ACTION,
            "verified": result.get("verified"),
            "verification": result.get("verification"),
        }

    # ------------------------------------------------------------------
    # Result builders (fixed text only; never echo input or service data)

    @staticmethod
    def _find_failure(
        code: str,
        *,
        message: str | None = None,
        candidates: list | None = None,
        count: int = 0,
        truncated: bool = False,
        bounds: dict | None = None,
    ) -> dict:
        bounded = list(candidates or [])
        return {
            "success": False,
            "code": code,
            "message": message if message is not None else _MESSAGES[code],
            "target": None,
            "matches": bounded,
            "candidates": bounded,
            "count": count,
            "truncated": truncated,
            "bounds": bounds,
        }

    @staticmethod
    def _activation_failure(
        code: str,
        *,
        message: str | None = None,
        target: dict | None = None,
        candidates: list | None = None,
        count: int = 0,
        truncated: bool = False,
        verified: bool | None = None,
        verification: dict | None = None,
        bounds: dict | None = None,
    ) -> dict:
        bounded = list(candidates or [])
        if verification is None:
            verification = {
                "performed": False,
                "status": "unavailable",
                "same_path": None,
                "path_present": None,
            }
        return {
            "success": False,
            "code": code,
            "message": message if message is not None else _MESSAGES[code],
            "target": target,
            "matches": bounded,
            "candidates": bounded,
            "count": count,
            "truncated": truncated,
            "action": PRESS_ACTION,
            "verified": verified,
            "verification": verification,
            "bounds": bounds,
        }

    # ------------------------------------------------------------------
    # Small helpers

    @staticmethod
    def _bounded_strings(value: Any, limit: int) -> list[str]:
        if not isinstance(value, (list, tuple)):
            return []
        items = []
        for item in value:
            if not isinstance(item, str):
                continue
            items.append(item[:MAX_NAME_LENGTH])
            if len(items) >= limit:
                break
        return items


__all__ = [
    "ControlActivator",
    "FAILURE_CODES",
    "MAX_CANDIDATES",
    "MAX_DEPTH",
    "MAX_NODES",
    "MAX_PROCESS_ID",
    "MAX_NAME_LENGTH",
    "MAX_ROLE_LENGTH",
]
