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


class ControlActivator:
    """Find one accessible control and, with approval, press it."""

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

    def find(
        self,
        process_id: int,
        name: str,
        role: str | None = None,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_nodes: int = DEFAULT_MAX_NODES,
    ) -> dict:
        """Find a unique control by name (and optional role), read-only."""
        params, error = self._validate_find(
            process_id, name, role, max_depth, max_nodes
        )
        bounds = {"max_depth": max_depth, "max_nodes": max_nodes}
        if error is not None:
            return self._find_failure(
                "invalid_input", message=error, bounds=bounds
            )

        process_id, query, role_filter, max_depth, max_nodes = params
        inspection = self._inspect(process_id, max_depth, max_nodes)
        if inspection is None:
            return self._find_failure("not_found", bounds=bounds)

        nodes = inspection.get("nodes")
        if not isinstance(nodes, list):
            return self._find_failure("not_found", bounds=bounds)

        actions_inspected = inspection.get("actions_inspected")
        if not isinstance(actions_inspected, int) or isinstance(
            actions_inspected, bool
        ):
            actions_inspected = 0

        candidates, total = self._match(
            nodes, query, role_filter, actions_inspected
        )
        truncated = inspection.get("truncated") is True

        if total == 0:
            return self._find_failure(
                "not_found", truncated=truncated, bounds=bounds
            )

        if total > 1:
            return self._find_failure(
                "ambiguous",
                candidates=candidates,
                count=total,
                truncated=truncated,
                bounds=bounds,
            )

        target = candidates[0]
        return {
            "success": True,
            "code": SUCCESS_CODE,
            "message": _MESSAGES["ok"],
            "target": target,
            "matches": [target],
            "candidates": [target],
            "count": 1,
            "truncated": truncated,
            "bounds": bounds,
        }

    def activate(
        self,
        process_id: int,
        name: str,
        role: str | None = None,
        confirmed: bool = False,
        expected_name: str | None = None,
    ) -> dict:
        """Press a unique control, but only after explicit confirmation."""
        params, error = self._validate_activate(
            process_id, name, role, confirmed, expected_name
        )
        if error is not None:
            return self._activation_failure("invalid_input", message=error)

        process_id, query, role_filter, expected = params
        if confirmed is not True:
            return self._activation_failure("approval_required")

        found = self.find(process_id, query, role=role_filter)
        if found["success"] is not True:
            return self._activation_failure(
                found["code"],
                candidates=found["candidates"],
                count=found["count"],
                truncated=found["truncated"],
                bounds=found["bounds"],
            )

        target = found["target"]
        if expected is not None and target["name"] != expected:
            return self._activation_failure(
                "stale_target",
                target=target,
                candidates=[target],
                count=1,
                truncated=found["truncated"],
                bounds=found["bounds"],
            )

        if target["available"] is not True:
            return self._activation_failure(
                "unsupported",
                target=target,
                candidates=[target],
                count=1,
                truncated=found["truncated"],
                bounds=found["bounds"],
            )

        perform = getattr(self._service, "perform_action", None)
        if not callable(perform):
            return self._activation_failure(
                "unsupported",
                target=target,
                candidates=[target],
                count=1,
                truncated=found["truncated"],
                bounds=found["bounds"],
            )

        guard = expected if expected is not None else target["name"]
        try:
            raw = perform(
                process_id=process_id,
                path=target["path"],
                action_name=PRESS_ACTION,
                confirmed=True,
                expected_name=guard,
            )
        except Exception:
            # Exception text can contain control names or tree content.
            return self._activation_failure(
                "action_failed",
                target=target,
                candidates=[target],
                count=1,
                truncated=found["truncated"],
                bounds=found["bounds"],
            )

        if not isinstance(raw, dict) or raw.get("success") is not True:
            return self._activation_failure(
                "action_failed",
                target=target,
                candidates=[target],
                count=1,
                truncated=found["truncated"],
                bounds=found["bounds"],
            )

        if raw.get("invoked") is False:
            return self._activation_failure(
                "action_failed",
                target=target,
                candidates=[target],
                count=1,
                truncated=found["truncated"],
                bounds=found["bounds"],
            )

        service_verification = raw.get("verification")
        if (
            isinstance(service_verification, dict)
            and service_verification.get("status") == "failed"
        ):
            return self._activation_failure(
                "verification_failed",
                target=target,
                candidates=[target],
                count=1,
                truncated=found["truncated"],
                verified=False,
                verification=self._verification_details(
                    performed=False,
                    status="failed",
                    same_path=None,
                    path_present=None,
                    truncated=False,
                ),
                bounds=found["bounds"],
            )

        verified, details = self._verify(process_id, target)
        code = SUCCESS_CODE if verified is not False else "verification_failed"
        if verified is True:
            message = (
                "The press was accepted and the control was re-identified "
                "at the same path."
            )
        elif verified is False:
            message = _MESSAGES["verification_failed"]
        else:
            message = (
                "The press was accepted, but post-action verification was "
                "unavailable."
            )

        return {
            "success": True,
            "code": code,
            "message": message,
            "target": target,
            "matches": [target],
            "candidates": [target],
            "count": 1,
            "truncated": found["truncated"],
            "action": PRESS_ACTION,
            "verified": verified,
            "verification": details,
            "bounds": found["bounds"],
        }

    # ------------------------------------------------------------------
    # Inspection and matching

    def _inspect(
        self, process_id: int, max_depth: int, max_nodes: int
    ) -> dict | None:
        """Run a bounded read-only inspection; return None on any failure."""
        inspect = getattr(self._inspector, "inspect_application", None)
        if not callable(inspect):
            return None
        try:
            result = inspect(
                process_id,
                max_depth,
                max_nodes,
                include_actions=True,
            )
        except Exception:
            return None
        if not isinstance(result, dict) or result.get("success") is not True:
            return None
        if not isinstance(result.get("nodes"), list):
            return None
        return result

    def _match(
        self,
        nodes: list,
        query: str,
        role_filter: str | None,
        actions_inspected: int,
    ) -> tuple[list[dict], int]:
        """Return bounded unique candidates and the total match count."""
        folded_query = query.casefold()
        folded_role = (
            None if role_filter is None else role_filter.casefold()
        )
        exact: list[dict] = []
        substring: list[dict] = []
        seen: set[str] = set()

        for raw in nodes:
            if not isinstance(raw, dict):
                continue

            path = raw.get("path")
            if (
                not isinstance(path, str)
                or not path
                or path == "0"
                or len(path) > MAX_PATH_LENGTH
            ):
                continue
            if path in seen:
                continue

            node_name = raw.get("name")
            if not isinstance(node_name, str):
                continue
            folded_name = node_name.strip().casefold()
            if not folded_name:
                continue

            node_role = raw.get("role")
            if not isinstance(node_role, str) or not node_role:
                node_role = "unknown"
            if folded_role is not None and node_role.casefold() != folded_role:
                continue

            seen.add(path)
            candidate = self._candidate(raw, actions_inspected)
            if folded_name == folded_query:
                exact.append(candidate)
            elif folded_query in folded_name:
                substring.append(candidate)

        matches = exact if exact else substring
        return matches[:MAX_CANDIDATES], len(matches)

    def _candidate(self, node: dict, actions_inspected: int) -> dict:
        states = self._bounded_strings(node.get("states"), MAX_STATES)
        actions = self._bounded_strings(node.get("actions"), MAX_ACTIONS)
        folded_states = {state.casefold() for state in states}
        states_ok = REQUIRED_STATES.issubset(folded_states)
        has_press = any(
            action.casefold() == PRESS_ACTION for action in actions
        )
        if actions_inspected:
            available = states_ok and has_press
        else:
            # The service may not expose action discovery.  Fall back to the
            # states the press itself requires; the service remains the final
            # authority and will refuse anything it cannot press.
            available = states_ok

        role = node.get("role")
        if not isinstance(role, str) or not role:
            role = "unknown"
        name = node.get("name")
        if not isinstance(name, str):
            name = ""

        return {
            "path": node.get("path")[:MAX_PATH_LENGTH],
            "role": role[:MAX_ROLE_LENGTH],
            "name": name[:MAX_NAME_LENGTH],
            "available": available,
            "states": states,
            "actions": actions,
        }

    # ------------------------------------------------------------------
    # Post-action verification

    def _verify(
        self, process_id: int, target: dict
    ) -> tuple[bool | None, dict]:
        """Re-inspect the same path and report whether it still matches."""
        inspection = self._inspect(
            process_id, DEFAULT_MAX_DEPTH, DEFAULT_MAX_NODES
        )
        if inspection is None:
            return None, self._verification_details(
                performed=False,
                status="unavailable",
                same_path=None,
                path_present=None,
                truncated=False,
            )

        truncated = inspection.get("truncated") is True
        node = None
        for raw in inspection.get("nodes", []):
            if isinstance(raw, dict) and raw.get("path") == target["path"]:
                node = raw
                break

        if node is None:
            if truncated:
                return None, self._verification_details(
                    performed=True,
                    status="unavailable",
                    same_path=None,
                    path_present=None,
                    truncated=True,
                )
            return False, self._verification_details(
                performed=True,
                status="failed",
                same_path=False,
                path_present=False,
                truncated=False,
            )

        node_name = node.get("name")
        node_role = node.get("role")
        same = (
            isinstance(node_name, str)
            and node_name == target["name"]
            and isinstance(node_role, str)
            and node_role == target["role"]
        )
        return same, self._verification_details(
            performed=True,
            status="verified" if same else "failed",
            same_path=True,
            path_present=True,
            truncated=truncated,
        )

    @staticmethod
    def _verification_details(
        *,
        performed: bool,
        status: str,
        same_path: bool | None,
        path_present: bool | None,
        truncated: bool,
    ) -> dict:
        return {
            "performed": performed,
            "status": status,
            "same_path": same_path,
            "path_present": path_present,
            "truncated": truncated,
        }

    # ------------------------------------------------------------------
    # Validation

    def _validate_find(
        self,
        process_id: Any,
        name: Any,
        role: Any,
        max_depth: Any,
        max_nodes: Any,
    ) -> tuple[tuple | None, str | None]:
        if not _valid_process_id(process_id):
            return None, _VALIDATION_MESSAGES["process_id"]

        query = _normalize_text(name, MAX_NAME_LENGTH)
        if query is None:
            return None, _VALIDATION_MESSAGES["name"]

        role_filter = None
        if role is not None:
            role_filter = _normalize_text(role, MAX_ROLE_LENGTH)
            if role_filter is None:
                return None, _VALIDATION_MESSAGES["role"]

        if not _valid_bounded_int(max_depth, MIN_DEPTH, MAX_DEPTH):
            return None, _VALIDATION_MESSAGES["max_depth"]
        if not _valid_bounded_int(max_nodes, MIN_NODES, MAX_NODES):
            return None, _VALIDATION_MESSAGES["max_nodes"]

        return (
            process_id,
            query,
            role_filter,
            max_depth,
            max_nodes,
        ), None

    def _validate_activate(
        self,
        process_id: Any,
        name: Any,
        role: Any,
        confirmed: Any,
        expected_name: Any,
    ) -> tuple[tuple | None, str | None]:
        params, error = self._validate_find(
            process_id,
            name,
            role,
            DEFAULT_MAX_DEPTH,
            DEFAULT_MAX_NODES,
        )
        if error is not None:
            return None, error

        if not isinstance(confirmed, bool):
            return None, _VALIDATION_MESSAGES["confirmed"]

        expected = None
        if expected_name is not None:
            expected = _normalize_text(expected_name, MAX_NAME_LENGTH)
            if expected is None:
                return None, _VALIDATION_MESSAGES["expected_name"]

        process_id, query, role_filter, _, _ = params
        return (process_id, query, role_filter, expected), None

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
            verification = ControlActivator._verification_details(
                performed=False,
                status="unavailable",
                same_path=None,
                path_present=None,
                truncated=False,
            )
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
