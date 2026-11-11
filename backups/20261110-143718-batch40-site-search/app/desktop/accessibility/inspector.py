"""Isolated, observation-only accessibility inspection adapter.

This adapter composes :class:`AccessibilityService` through a small,
duck-typed interface. It calls only the service's listing and inspection
methods, flattens the result into bounded plain data, and never invokes an
accessibility action.

The module deliberately has no registry wiring, no Qt imports, no model
imports, and no logging of accessible content.
"""

from __future__ import annotations

from typing import Any

from app.desktop.accessibility.service import AccessibilityService


MIN_DEPTH = 1
MAX_DEPTH = 32
MIN_NODES = 1
MAX_NODES = 5000
MAX_SELECTOR_LENGTH = 256
MAX_PROCESS_ID = 2_147_483_647
MAX_APPLICATIONS = 512
MAX_STATES = 64
MAX_ACTIONS = 64
MAX_NAME_LENGTH = 512
MAX_DESCRIPTION_LENGTH = 2000
MAX_ROLE_LENGTH = 128
MAX_STATE_LENGTH = 80
MAX_ACTION_NAME_LENGTH = 200
MAX_ATTRIBUTE_COUNT = 64
MAX_ATTRIBUTE_LENGTH = 256

# AccessibilityService bounds its own traversal at depth 20. The adapter
# accepts a wider bound but must not pass an unsupported value through.
SERVICE_MAX_DEPTH = 20


class AccessibilityInspector:
    """Bounded, read-only view over an accessibility service."""

    def __init__(self, service: Any | None = None):
        self._service = (
            AccessibilityService() if service is None else service
        )

    def list_applications(self) -> dict:
        """List accessible applications as bounded, plain records."""
        try:
            result = self._service.list_applications()
        except Exception:
            return self._list_failure(
                "The accessibility service could not list applications."
            )

        if not isinstance(result, dict) or result.get("success") is not True:
            return self._list_failure(
                "The accessibility service could not list applications."
            )

        raw_applications = result.get("applications")
        if not isinstance(raw_applications, list):
            return self._list_failure(
                "The accessibility service returned an invalid application list."
            )

        applications = []
        for raw in raw_applications:
            record = self._application_record(raw)
            if record is not None:
                applications.append(record)
            if len(applications) >= MAX_APPLICATIONS:
                break

        truncated = (
            len(applications) >= MAX_APPLICATIONS
            and len(raw_applications) > len(applications)
        )
        reasons = ["applications"] if truncated else []

        return {
            "success": True,
            "applications": applications,
            "count": len(applications),
            "truncated": truncated,
            "truncation": {
                "truncated": truncated,
                "reasons": reasons,
                "application_limit": MAX_APPLICATIONS,
            },
            "message": "Accessible applications discovered.",
        }

    def inspect_application(
        self,
        selector: Any,
        max_depth: int = 8,
        max_nodes: int = 500,
        include_actions: bool = True,
    ) -> dict:
        """Inspect one application and return a flat, bounded node list.

        ``selector`` may be a positive process id, a non-empty application
        name, or a dict containing exactly one of ``process_id`` or ``name``.
        """
        selector_spec, selector_error = self._validate_selector(selector)
        if selector_error is not None:
            return self._inspect_failure(selector_error)

        if not self._valid_bounded_int(max_depth, MIN_DEPTH, MAX_DEPTH):
            return self._inspect_failure(
                f"max_depth must be an integer between {MIN_DEPTH} and {MAX_DEPTH}."
            )

        if not self._valid_bounded_int(max_nodes, MIN_NODES, MAX_NODES):
            return self._inspect_failure(
                f"max_nodes must be an integer between {MIN_NODES} and {MAX_NODES}."
            )

        if not isinstance(include_actions, bool):
            return self._inspect_failure(
                "include_actions must be a boolean.",
                max_depth=max_depth,
                max_nodes=max_nodes,
            )

        listing = self.list_applications()
        if not listing["success"]:
            return self._inspect_failure(
                listing["message"],
                max_depth=max_depth,
                max_nodes=max_nodes,
            )

        matches = self._matching_applications(
            listing["applications"], selector_spec
        )
        if not matches:
            return self._inspect_failure(
                "No matching accessible application was found.",
                max_depth=max_depth,
                max_nodes=max_nodes,
            )
        if len(matches) > 1:
            if "name" in selector_spec:
                message = "Multiple applications matched; use a process_id."
            else:
                message = "Multiple applications matched; use an application name."
            return self._inspect_failure(
                message,
                max_depth=max_depth,
                max_nodes=max_nodes,
            )

        application = matches[0]
        try:
            inspection = self._inspect_service(application, max_depth, max_nodes)
        except Exception:
            return self._inspect_failure(
                "The accessibility service could not inspect the application.",
                application=application,
                max_depth=max_depth,
                max_nodes=max_nodes,
            )

        if (
            not isinstance(inspection, dict)
            or inspection.get("success") is not True
        ):
            return self._inspect_failure(
                "The accessibility service could not inspect the application.",
                application=application,
                max_depth=max_depth,
                max_nodes=max_nodes,
            )

        tree = inspection.get("tree")
        if not isinstance(tree, dict):
            return self._inspect_failure(
                "The accessibility service returned an invalid accessibility tree.",
                application=application,
                max_depth=max_depth,
                max_nodes=max_nodes,
            )

        nodes, node_reasons, actions_inspected = self._flatten(
            tree,
            application=application,
            max_depth=max_depth,
            max_nodes=max_nodes,
            include_actions=include_actions,
        )

        reasons = set(node_reasons)
        if inspection.get("truncated") is True and not reasons:
            reasons.add("service")

        truncated = bool(reasons)
        return {
            "success": True,
            "application": application,
            "nodes": nodes,
            "count": len(nodes),
            "actions_inspected": actions_inspected,
            "truncated": truncated,
            "truncation": {
                "truncated": truncated,
                "reasons": sorted(reasons),
                "depth_limit": max_depth,
                "node_limit": max_nodes,
                "service_depth_limit": min(max_depth, SERVICE_MAX_DEPTH),
            },
            "message": "Accessibility tree inspected (read-only).",
        }

    # ------------------------------------------------------------------
    # Service interaction

    def _inspect_service(
        self, application: dict, max_depth: int, max_nodes: int
    ) -> Any:
        """Call the service's inspection method with bounded arguments."""
        service_depth = min(max_depth, SERVICE_MAX_DEPTH)
        process_id = application.get("process_id")
        if process_id is not None:
            return self._service.inspect_application(
                process_id=process_id,
                max_depth=service_depth,
                max_nodes=max_nodes,
            )
        return self._service.inspect_application(
            name=application["name"],
            max_depth=service_depth,
            max_nodes=max_nodes,
        )

    def _action_names(self, process_id: int | None, path: str) -> list[str]:
        """Read action names, if the service exposes a read-only lister."""
        if process_id is None:
            return []

        lister = getattr(self._service, "list_actions", None)
        if not callable(lister):
            return []

        try:
            result = lister(process_id=process_id, path=path)
        except Exception:
            return []

        if not isinstance(result, dict) or result.get("success") is not True:
            return []

        raw_actions = result.get("actions")
        if not isinstance(raw_actions, list):
            return []

        names = []
        seen = set()
        for raw in raw_actions:
            if isinstance(raw, dict):
                name = self._text(
                    raw.get("name"), MAX_ACTION_NAME_LENGTH
                )
            else:
                name = self._text(raw, MAX_ACTION_NAME_LENGTH)
            if name and name not in seen:
                seen.add(name)
                names.append(name)
            if len(names) >= MAX_ACTIONS:
                break
        return names

    # ------------------------------------------------------------------
    # Tree flattening

    def _flatten(
        self,
        tree: dict,
        *,
        application: dict,
        max_depth: int,
        max_nodes: int,
        include_actions: bool,
    ) -> tuple[list[dict], set[str], int]:
        """Flatten a service tree pre-order with positional paths."""
        nodes: list[dict] = []
        all_reasons: set[str] = set()
        process_id = application.get("process_id")
        actions_inspected = 0

        def walk(
            raw: dict,
            depth: int,
            parent_path: str | None,
            child_index: int | None,
        ) -> None:
            nonlocal actions_inspected
            path = (
                "0"
                if parent_path is None
                else f"{parent_path}/{child_index}"
            )

            raw_children = raw.get("children")
            children_were_malformed = False
            if raw_children is None:
                raw_children = []
            elif not isinstance(raw_children, list):
                raw_children = []
                children_were_malformed = True

            malformed_children = any(
                not isinstance(child, dict) for child in raw_children
            )

            declared_children = self._bounded_int(
                raw.get("child_count"),
                minimum=0,
                maximum=1_000_000,
                default=len(raw_children),
            )
            child_count = max(declared_children, len(raw_children))

            record = {
                "path": self._text(path, 256),
                "parent_path": (
                    None
                    if parent_path is None
                    else self._text(parent_path, 256)
                ),
                "child_index": child_index,
                "depth": depth,
                "role": self._text(
                    raw.get("role"), MAX_ROLE_LENGTH, default="unknown"
                )
                or "unknown",
                "name": self._text(raw.get("name"), MAX_NAME_LENGTH),
                "description": self._text(
                    raw.get("description"), MAX_DESCRIPTION_LENGTH
                ),
                "states": self._states(raw.get("states")),
                "actions": [],
                "attributes": self._attributes(raw.get("attributes")),
                "child_count": child_count,
                "children_included": 0,
                "truncated": False,
                "truncation_reasons": [],
            }

            node_reasons: set[str] = set()
            if children_were_malformed or malformed_children:
                node_reasons.add("malformed")

            if include_actions:
                record["actions"] = self._action_names(process_id, path)
                if process_id is not None:
                    actions_inspected += 1

            nodes.append(record)

            if child_count:
                if depth >= max_depth:
                    node_reasons.add("depth")
                else:
                    for index, child in enumerate(raw_children):
                        if not isinstance(child, dict):
                            continue
                        if len(nodes) >= max_nodes:
                            node_reasons.add("nodes")
                            break
                        walk(child, depth + 1, path, index)
                        record["children_included"] += 1

                    service_reported = (
                        raw.get("truncated") is True
                        or child_count > len(raw_children)
                    )
                    if service_reported and not node_reasons:
                        node_reasons.add("service")
            elif raw.get("truncated") is True:
                node_reasons.add("service")

            record["truncation_reasons"] = sorted(node_reasons)
            record["truncated"] = bool(record["truncation_reasons"])
            if record["truncated"]:
                all_reasons.update(record["truncation_reasons"])

        walk(tree, 0, None, None)
        return nodes, all_reasons, actions_inspected

    # ------------------------------------------------------------------
    # Validation and sanitising

    def _validate_selector(self, selector: Any) -> tuple[dict | None, str | None]:
        """Normalise a selector without touching the service."""
        if isinstance(selector, bool):
            return None, "selector must be a process id or an application name."

        if isinstance(selector, int):
            if not 1 <= selector <= MAX_PROCESS_ID:
                return None, (
                    f"process_id must be between 1 and {MAX_PROCESS_ID}."
                )
            return {"process_id": selector}, None

        if isinstance(selector, str):
            error = self._name_error(selector)
            if error is not None:
                return None, error
            return {"name": selector.strip()}, None

        if isinstance(selector, dict):
            if set(selector) == {"process_id"}:
                process_id = selector.get("process_id")
                if (
                    isinstance(process_id, bool)
                    or not isinstance(process_id, int)
                    or not 1 <= process_id <= MAX_PROCESS_ID
                ):
                    return None, (
                        f"process_id must be between 1 and {MAX_PROCESS_ID}."
                    )
                return {"process_id": process_id}, None
            if set(selector) == {"name"}:
                name = selector.get("name")
                if not isinstance(name, str):
                    return None, "name must be a string."
                error = self._name_error(name)
                if error is not None:
                    return None, error
                return {"name": name.strip()}, None
            return None, (
                "selector must be a process id, an application name, or a dict "
                "with exactly one of 'process_id' or 'name'."
            )

        return None, "selector must be a process id or an application name."

    @staticmethod
    def _name_error(name: str) -> str | None:
        if len(name) > MAX_SELECTOR_LENGTH:
            return (
                f"Application name must be at most {MAX_SELECTOR_LENGTH} characters."
            )
        stripped = name.strip()
        if not stripped:
            return "Application name must not be empty."
        if "\x00" in stripped:
            return "Application name must not contain NUL characters."
        return None

    @staticmethod
    def _valid_bounded_int(value: Any, minimum: int, maximum: int) -> bool:
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and minimum <= value <= maximum
        )

    @staticmethod
    def _matching_applications(
        applications: list[dict], selector: dict
    ) -> list[dict]:
        if "process_id" in selector:
            process_id = selector["process_id"]
            return [
                item
                for item in applications
                if item.get("process_id") == process_id
            ]
        name = selector["name"]
        return [
            item for item in applications if item.get("name") == name
        ]

    @classmethod
    def _application_record(cls, raw: Any) -> dict | None:
        if not isinstance(raw, dict):
            return None
        if raw.get("inspection_error"):
            return None

        name = cls._text(raw.get("name"), MAX_NAME_LENGTH)
        process_id = cls._bounded_int(
            raw.get("process_id"),
            minimum=1,
            maximum=MAX_PROCESS_ID,
            default=None,
        )
        if not name and process_id is None:
            return None

        role = cls._text(
            raw.get("role"), MAX_ROLE_LENGTH, default="unknown"
        )
        return {
            "name": name,
            "role": role or "unknown",
            "process_id": process_id,
            "desktop_index": cls._bounded_int(
                raw.get("desktop_index"),
                minimum=0,
                maximum=1_000_000,
                default=None,
            ),
            "accessible_id": cls._text(
                raw.get("accessible_id"), MAX_NAME_LENGTH
            ),
            "child_count": cls._bounded_int(
                raw.get("child_count"),
                minimum=0,
                maximum=1_000_000,
                default=0,
            ),
        }

    @classmethod
    def _states(cls, raw: Any) -> list[str]:
        if not isinstance(raw, (list, tuple)):
            return []
        states = []
        seen = set()
        for item in raw:
            state = cls._text(item, MAX_STATE_LENGTH)
            if state and state not in seen:
                seen.add(state)
                states.append(state)
            if len(states) >= MAX_STATES:
                break
        return states

    @classmethod
    def _attributes(cls, raw: Any) -> dict[str, str]:
        if not isinstance(raw, dict):
            return {}
        attributes = {}
        for key, value in raw.items():
            if len(attributes) >= MAX_ATTRIBUTE_COUNT:
                break
            attributes[cls._text(key, MAX_ATTRIBUTE_LENGTH)] = cls._text(
                value, MAX_ATTRIBUTE_LENGTH
            )
        return attributes

    @staticmethod
    def _text(value: Any, limit: int, default: str = "") -> str:
        if value is None:
            return default
        try:
            return str(value)[:limit]
        except Exception:
            return default

    @staticmethod
    def _bounded_int(
        value: Any,
        *,
        minimum: int,
        maximum: int,
        default: int | None,
    ) -> int | None:
        if isinstance(value, bool):
            return default
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            return default
        if not minimum <= number <= maximum:
            return default
        return number

    @staticmethod
    def _list_failure(message: str) -> dict:
        return {
            "success": False,
            "applications": [],
            "count": 0,
            "truncated": False,
            "truncation": {
                "truncated": False,
                "reasons": [],
                "application_limit": MAX_APPLICATIONS,
            },
            "message": message,
        }

    @staticmethod
    def _inspect_failure(
        message: str,
        *,
        application: dict | None = None,
        max_depth: int | None = None,
        max_nodes: int | None = None,
    ) -> dict:
        return {
            "success": False,
            "application": application,
            "nodes": [],
            "count": 0,
            "truncated": False,
            "truncation": {
                "truncated": False,
                "reasons": [],
                "depth_limit": max_depth,
                "node_limit": max_nodes,
                "service_depth_limit": (
                    None
                    if max_depth is None
                    else min(max_depth, SERVICE_MAX_DEPTH)
                ),
            },
            "message": message,
        }
