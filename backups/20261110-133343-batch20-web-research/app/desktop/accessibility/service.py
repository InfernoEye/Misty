"""Bounded, read-only AT-SPI inspection of desktop applications."""

from __future__ import annotations

from typing import Any


class AccessibilityService:
    """Discover applications and inspect their accessible UI hierarchy."""

    def __init__(self, desktop_provider: Any | None = None):
        self._desktop_provider = desktop_provider
        self._desktop = None
        self._atspi = None

    @property
    def connected(self) -> bool:
        return self._desktop is not None

    def connect(self) -> dict:
        """Connect to AT-SPI without changing system accessibility settings."""
        try:
            if self._desktop_provider is None:
                import gi
                gi.require_version("Atspi", "2.0")
                from gi.repository import Atspi

                self._atspi = Atspi
                desktop = Atspi.get_desktop(0)
            else:
                desktop = self._desktop_provider()

            if desktop is None:
                raise RuntimeError("AT-SPI returned no desktop object.")

            desktop.get_child_count()
            self._desktop = desktop

            return {
                "success": True,
                "message": "Connected to the AT-SPI desktop.",
            }
        except Exception as error:
            self._desktop = None
            return {
                "success": False,
                "message": f"Could not connect to AT-SPI: {error}",
            }

    def disconnect(self) -> None:
        """Release our reference; does not stop or modify the AT-SPI bus."""
        self._desktop = None
        self._atspi = None

    @staticmethod
    def _value(node: Any, method_name: str, default: Any = None) -> Any:
        try:
            method = getattr(node, method_name, None)
            if not callable(method):
                return default
            value = method()
            return default if value is None else value
        except Exception:
            return default

    @staticmethod
    def _state_name(state: Any) -> str:
        """Convert GI enum states and test doubles into readable names."""
        for attribute in ("value_nick", "name", "value_name"):
            try:
                value = getattr(state, attribute, None)
                if value is not None:
                    text = str(value).rsplit(".", 1)[-1].lower()
                    if text and not text.isdigit():
                        if text.startswith("atspi_state_"):
                            text = text[len("atspi_state_"):]
                        return text
            except Exception:
                pass

        import re

        rendered = repr(state)
        match = re.search(
            r"(?:StateType|STATE_TYPE)\.([A-Z][A-Z0-9_]*)",
            rendered,
        )
        if match:
            return match.group(1).lower()

        text = str(state).rsplit(".", 1)[-1].lower()
        return f"state_{text}" if text.isdigit() else text

    @classmethod
    def _describe(cls, node: Any) -> dict:
        """Read metadata from one accessible object. Never invoke actions."""
        name = cls._value(node, "get_name", "")
        description = cls._value(node, "get_description", "")
        role = cls._value(node, "get_role_name", "unknown")
        pid = cls._value(node, "get_process_id")
        accessible_id = cls._value(node, "get_accessible_id", "")
        child_count = cls._value(node, "get_child_count", 0)
        attributes = cls._value(node, "get_attributes", {})
        interfaces = cls._value(node, "get_interfaces", [])

        states = []
        try:
            state_set = node.get_state_set()
            raw_states = state_set.get_states() if state_set else []
            states = [
                cls._state_name(state)
                for state in raw_states
            ]
        except Exception:
            pass

        if not isinstance(attributes, dict):
            try:
                attributes = dict(attributes)
            except Exception:
                attributes = {}

        try:
            interfaces = [str(item) for item in interfaces]
        except Exception:
            interfaces = []

        try:
            pid = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            pid = None

        try:
            child_count = max(0, int(child_count or 0))
        except (TypeError, ValueError):
            child_count = 0

        return {
            "name": str(name)[:1000],
            "description": str(description)[:2000],
            "role": str(role),
            "process_id": pid,
            "accessible_id": str(accessible_id),
            "child_count": child_count,
            "states": states,
            "attributes": {
                str(key): str(value)[:1000]
                for key, value in attributes.items()
            },
            "interfaces": interfaces,
        }

    def list_applications(self) -> dict:
        """Return metadata for applications registered on the AT-SPI bus."""
        if self._desktop is None:
            connection = self.connect()
            if not connection["success"]:
                return {**connection, "applications": [], "count": 0}

        try:
            applications = []
            count = self._desktop.get_child_count()

            for index in range(count):
                try:
                    app = self._desktop.get_child_at_index(index)
                    if app is None:
                        continue

                    item = self._describe(app)
                    item["desktop_index"] = index
                    applications.append(item)
                except Exception as error:
                    applications.append({
                        "desktop_index": index,
                        "inspection_error": str(error),
                    })

            return {
                "success": True,
                "applications": applications,
                "count": len(applications),
                "message": "Accessible applications discovered.",
            }
        except Exception as error:
            return {
                "success": False,
                "applications": [],
                "count": 0,
                "message": f"Could not list applications: {error}",
            }

    def inspect_application(
        self,
        *,
        process_id: int | None = None,
        name: str | None = None,
        max_depth: int = 8,
        max_nodes: int = 500,
    ) -> dict:
        """Inspect one application by PID or exact name.

        Duplicate names are rejected as ambiguous. Traversal is bounded.
        """
        if (process_id is None) == (name is None):
            return {
                "success": False,
                "message": "Specify exactly one of process_id or name.",
                "tree": None,
            }

        if not isinstance(max_depth, int) or not 0 <= max_depth <= 20:
            return {
                "success": False,
                "message": "max_depth must be between 0 and 20.",
                "tree": None,
            }

        if not isinstance(max_nodes, int) or not 1 <= max_nodes <= 5000:
            return {
                "success": False,
                "message": "max_nodes must be between 1 and 5000.",
                "tree": None,
            }

        listing = self.list_applications()
        if not listing["success"]:
            return {**listing, "tree": None}

        matches = [
            item for item in listing["applications"]
            if "inspection_error" not in item
            and (
                item.get("process_id") == process_id
                if process_id is not None
                else item.get("name") == name
            )
        ]

        if not matches:
            return {
                "success": False,
                "message": "No matching accessible application was found.",
                "tree": None,
            }

        if len(matches) > 1:
            return {
                "success": False,
                "message": "Multiple applications matched; use process_id.",
                "matches": matches,
                "tree": None,
            }

        target = matches[0]

        try:
            root = self._desktop.get_child_at_index(
                target["desktop_index"]
            )
            if root is None:
                raise RuntimeError("Application disappeared from AT-SPI.")

            visited = 0
            errors = []

            def walk(node: Any, depth: int, path: str) -> dict:
                nonlocal visited
                visited += 1

                record = self._describe(node)
                record["path"] = path
                record["focused"] = self._focused_state(node)
                record["depth"] = depth
                record["children"] = []

                if depth >= max_depth or visited >= max_nodes:
                    if record["child_count"]:
                        record["truncated"] = True
                    return record

                for index in range(record["child_count"]):
                    if visited >= max_nodes:
                        record["truncated"] = True
                        break

                    child_path = f"{path}/{index}"

                    try:
                        child = node.get_child_at_index(index)
                        if child is not None:
                            record["children"].append(
                                walk(child, depth + 1, child_path)
                            )
                    except Exception as error:
                        errors.append({
                            "path": child_path,
                            "message": str(error),
                        })

                return record

            tree = walk(root, 0, "0")

            return {
                "success": True,
                "application": target,
                "tree": tree,
                "nodes_inspected": visited,
                "truncated": (
                    bool(tree.get("truncated"))
                    or visited >= max_nodes
                ),
                "errors": errors,
                "message": "Accessibility tree inspected (read-only).",
            }

        except Exception as error:
            return {
                "success": False,
                "application": target,
                "tree": None,
                "message": f"Could not inspect application: {error}",
            }

    def _focused_state(self, node: Any) -> bool | None:
        """Return the live AT-SPI focused state, or None if unavailable."""
        if self._atspi is None:
            return None
        try:
            state_set = node.get_state_set()
            if state_set is None:
                return None
            return bool(
                state_set.contains(self._atspi.StateType.FOCUSED)
            )
        except Exception:
            return None

    def _resolve_action_target(
        self, process_id: int, path: str,
        expected_name: str | None = None,
    ):
        """Resolve an accessible node afresh using PID and tree path."""
        if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0:
            return None, None, "process_id must be a positive integer."
        if not isinstance(path, str) or not path or len(path) > 200:
            return None, None, "A valid accessible tree path is required."

        parts = path.split("/")
        if parts[0] != "0" or any(not part.isdecimal() for part in parts):
            return None, None, "Tree path must start at application root '0'."
        if len(parts) > 21:
            return None, None, "Tree path exceeds maximum depth."

        listing = self.list_applications()
        if not listing.get("success"):
            return None, None, listing.get("message", "Could not list applications.")

        matches = [
            item for item in listing["applications"]
            if "inspection_error" not in item
            and item.get("process_id") == process_id
        ]
        if len(matches) != 1:
            return None, None, (
                "No matching application was found."
                if not matches else "Application PID is ambiguous."
            )

        application = matches[0]
        try:
            node = self._desktop.get_child_at_index(application["desktop_index"])
            if node is None:
                return None, application, "Application disappeared from AT-SPI."

            for raw_index in parts[1:]:
                index = int(raw_index)
                if index >= int(node.get_child_count()):
                    return None, application, "Tree path no longer identifies a control."
                node = node.get_child_at_index(index)
                if node is None:
                    return None, application, "Control disappeared from AT-SPI."

            description = self._describe(node)
            if expected_name is not None and description["name"] != expected_name:
                return None, application, "Control name changed; refusing to act on a different target."
            return node, application, None
        except Exception as error:
            return None, application, f"Could not resolve control: {error}"

    @staticmethod
    def _action_interface(node):
        for method_name in ("get_action_iface", "get_action"):
            try:
                method = getattr(node, method_name, None)
                if callable(method):
                    interface = method()
                    if interface is not None:
                        return interface
            except Exception:
                pass
        return None

    @classmethod
    def _available_actions(cls, node):
        interface = cls._action_interface(node)
        if interface is None:
            return []

        count = int(interface.get_n_actions())
        if not 0 <= count <= 64:
            raise RuntimeError("AT-SPI returned an invalid action count.")

        actions = []
        for index in range(count):
            # Canonical action identifier. Some GI releases mark this
            # getter deprecated, but expose no non-localized replacement.
            name = str(interface.get_action_name(index) or "")
            try:
                localized = str(interface.get_localized_name(index) or "")
            except Exception:
                localized = ""
            try:
                description = str(interface.get_action_description(index) or "")
            except Exception:
                description = ""
            actions.append({
                "index": index,
                "name": name,
                "localized_name": localized,
                "description": description,
            })
        return actions

    def list_actions(
        self, *, process_id: int, path: str,
        expected_name: str | None = None,
    ) -> dict:
        """List a control's AT-SPI actions without invoking them."""
        node, application, error = self._resolve_action_target(
            process_id, path, expected_name
        )
        if error:
            return {"success": False, "actions": [], "message": error}
        try:
            return {
                "success": True,
                "application": application,
                "control": self._describe(node),
                "path": path,
                "actions": self._available_actions(node),
                "message": "Available control actions inspected (read-only).",
            }
        except Exception as error:
            return {
                "success": False,
                "actions": [],
                "message": f"Could not inspect control actions: {error}",
            }

    def perform_action(
        self, *, process_id: int, path: str, action_name: str,
        confirmed: bool = False, expected_name: str | None = None,
    ) -> dict:
        """Invoke an allowlisted action on a freshly resolved control."""
        if not isinstance(action_name, str):
            return {"success": False, "message": "action_name must be a string."}

        allowed = {"press": "Press", "set focus": "Set Focus"}
        canonical = allowed.get(action_name.strip().casefold())
        if canonical is None:
            return {
                "success": False,
                "message": "Action is not allowlisted. Allowed actions: Press, Set Focus.",
            }
        if canonical == "Press" and confirmed is not True:
            return {
                "success": False,
                "requires_confirmation": True,
                "message": "Press requires explicit confirmation.",
            }
        if not isinstance(confirmed, bool):
            return {"success": False, "message": "confirmed must be a boolean."}

        node, application, error = self._resolve_action_target(
            process_id, path, expected_name
        )
        if error:
            return {"success": False, "message": error}

        control = self._describe(node)
        states = set(control.get("states", []))
        required = {"enabled", "sensitive", "visible"}
        if not required.issubset(states):
            missing = sorted(required - states)
            return {
                "success": False,
                "message": "Control is not actionable; missing states: " + ", ".join(missing),
            }

        try:
            actions = self._available_actions(node)
            matches = [
                item for item in actions
                if item["name"].casefold() == canonical.casefold()
            ]
            if len(matches) != 1:
                return {
                    "success": False,
                    "message": f"Control does not expose exactly one '{canonical}' action.",
                }

            interface = self._action_interface(node)
            invoked = bool(interface.do_action(matches[0]["index"]))

            if not invoked:
                verification = {
                    "status": "failed",
                    "focused": None,
                    "message": "AT-SPI did not accept the action.",
                }
            elif canonical == "Set Focus":
                focused = self._focused_state(node)
                verification = {
                    "status": "verified" if focused is True else "unverified",
                    "focused": focused,
                    "message": (
                        "The focused state was observed."
                        if focused is True
                        else "AT-SPI accepted the request, but focus was not independently confirmed."
                    ),
                }
            else:
                verification = {
                    "status": "unverified",
                    "focused": None,
                    "message": "The action was accepted; its resulting UI state has not been independently checked.",
                }

            return {
                "success": invoked,
                "invoked": invoked,
                "verification": verification,
                "application": application,
                "control": control,
                "path": path,
                "action": canonical,
                "message": (
                    "AT-SPI action invoked."
                    if invoked else "AT-SPI reported that the action was not performed."
                ),
            }
        except Exception as error:
            return {
                "success": False,
                "message": f"Could not invoke AT-SPI action: {error}",
            }

