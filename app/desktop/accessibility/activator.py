"""Semantic accessibility activation with bounded lookup and approval.

The model names an application and a control. This adapter inspects the
existing read-only AT-SPI tree, requires a unique match, then delegates the
Press action to AccessibilityService with a stale-name guard. It never
invents a control, never guesses among candidates, and never logs names.
"""

from __future__ import annotations

from app.desktop.accessibility.service import AccessibilityService


MAX_NAME_CHARS = 256
MAX_DEPTH = 8
MAX_NODES = 500
MAX_CANDIDATES = 10

_MESSAGES = {
    "invalid_input": "Application and control names are required.",
    "approval_required": "Pressing a control requires explicit approval.",
    "not_found": "No matching control was found.",
    "ambiguous": "Several controls match; refine the name or role.",
    "action_failed": "The control action failed.",
    "stale_target": "The control changed before it could be pressed.",
    "verification_failed": "The press was accepted but could not be verified.",
    "unsupported": "The accessibility service is unavailable.",
}


# AccessibilityService._resolve_action_target reports a label mismatch with
# this fixed message and no result code; a held-back press is flagged with
# ``requires_confirmation``. Both mean "not the control that was found".
_STALE_MESSAGE = "Control name changed"
_STALE_CODES = frozenset({"stale_target"})


def _is_stale_refusal(result):
    """True when the service refused because the target changed."""
    if not isinstance(result, dict):
        return False
    if result.get("requires_confirmation") is True:
        return True
    if result.get("code") in _STALE_CODES:
        return True
    message = result.get("message")
    return isinstance(message, str) and message.startswith(_STALE_MESSAGE)


def _failure(code, message=None, **extra):
    return {
        "success": False,
        "code": code,
        "message": message or _MESSAGES.get(code, "Accessibility action failed."),
        **extra,
    }


class ControlActivator:
    def __init__(self, service=None):
        self.service = service or AccessibilityService()

    @staticmethod
    def _valid_name(value):
        return (
            isinstance(value, str)
            and bool(value.strip())
            and len(value) <= MAX_NAME_CHARS
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        )

    def _inspect(self, process_id, app_name):
        if process_id is not None:
            result = self.service.inspect_application(
                process_id=process_id,
                max_depth=MAX_DEPTH,
                max_nodes=MAX_NODES,
            )
        else:
            result = self.service.inspect_application(
                name=app_name,
                max_depth=MAX_DEPTH,
                max_nodes=MAX_NODES,
            )
        return result

    def find(self, process_id=None, app_name=None, name=None, role=None):
        if not self._valid_name(name):
            return _failure("invalid_input")
        if (process_id is None) == (app_name is None):
            return _failure("invalid_input")
        if process_id is not None and (
            not isinstance(process_id, int)
            or isinstance(process_id, bool)
            or process_id < 1
        ):
            return _failure("invalid_input")
        if app_name is not None and not self._valid_name(app_name):
            return _failure("invalid_input")
        if role is not None and not self._valid_name(role):
            return _failure("invalid_input")

        inspected = self._inspect(process_id, app_name)
        if inspected.get("success") is not True or not isinstance(
            inspected.get("tree"), dict
        ):
            return _failure("not_found")

        wanted = name.strip().casefold()
        wanted_role = role.strip().casefold() if role else None
        exact = []
        partial = []

        stack = [inspected["tree"]]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            node_role = str(node.get("role", "")).casefold()
            node_name = str(node.get("name", "")).strip()
            if wanted_role is None or node_role == wanted_role:
                folded = node_name.casefold()
                if folded == wanted:
                    exact.append(node)
                elif wanted in folded:
                    partial.append(node)
            children = node.get("children")
            if isinstance(children, list):
                stack.extend(reversed(children))

        matches = exact or partial
        if not matches:
            return _failure("not_found")
        if len(matches) > 1:
            candidates = [
                {
                    "name": str(node.get("name", ""))[:MAX_NAME_CHARS],
                    "role": str(node.get("role", ""))[:64],
                    "path": str(node.get("path", ""))[:64],
                }
                for node in matches[:MAX_CANDIDATES]
            ]
            return _failure(
                "ambiguous",
                candidates=candidates,
                count=len(matches),
            )

        node = matches[0]
        return {
            "success": True,
            "code": "ok",
            "message": "One matching control found.",
            "control": {
                "name": str(node.get("name", ""))[:MAX_NAME_CHARS],
                "role": str(node.get("role", ""))[:64],
                "path": str(node.get("path", ""))[:64],
                "states": list(node.get("states") or [])[:32],
            },
        }

    def activate(
        self,
        process_id=None,
        app_name=None,
        name=None,
        role=None,
        confirmed=False,
        expected_name=None,
    ):
        if confirmed is not True:
            return _failure("approval_required")
        found = self.find(
            process_id=process_id,
            app_name=app_name,
            name=name,
            role=role,
        )
        if not found.get("success"):
            return found

        control = found["control"]
        expected = expected_name if isinstance(expected_name, str) else control["name"]
        if process_id is not None:
            target_kwargs = {"process_id": process_id}
        else:
            # The service's perform_action needs a PID; resolve it from the
            # inspected application metadata.
            inspected = self._inspect(process_id, app_name)
            application = inspected.get("application") or {}
            pid = application.get("process_id")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
                return _failure("stale_target")
            target_kwargs = {"process_id": pid}

        try:
            result = self.service.perform_action(
                **target_kwargs,
                path=control["path"],
                action_name="press",
                confirmed=True,
                expected_name=expected,
            )
        except Exception:
            return _failure("action_failed")

        if result.get("success") is not True:
            if _is_stale_refusal(result):
                return _failure("stale_target")
            return _failure("action_failed")

        verification = result.get("verification") or {}
        status = verification.get("status") if isinstance(verification, dict) else None
        verified = True if status == "verified" else (False if status == "failed" else None)
        return {
            "success": True,
            "code": "ok",
            "message": "Control pressed; resulting state was not independently verified."
            if verified is None
            else "Control pressed.",
            "control": control,
            "verified": verified,
            "verification": {
                "status": str(status or "unverified")[:32],
            },
        }
