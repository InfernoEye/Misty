import ast
import copy
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry
from app.desktop.accessibility import actions as legacy_actions
from app.desktop.accessibility.activator import ControlActivator


class FakeService:
    def __init__(self, tree, process_id=4321):
        self.tree = tree
        self.process_id = process_id
        self.inspections = []
        self.actions = []
        self.action_result = {
            "success": True,
            "verification": {"status": "unverified"},
        }

    def inspect_application(self, **kwargs):
        self.inspections.append(kwargs)
        return {
            "success": True,
            "application": {"process_id": self.process_id, "name": "App"},
            "tree": self.tree,
        }

    def perform_action(self, **kwargs):
        self.actions.append(kwargs)
        return dict(self.action_result)


def node(name, role="push button", path="0", children=None, states=None):
    return {
        "name": name,
        "role": role,
        "path": path,
        "children": children or [],
        "states": states or ["enabled", "sensitive", "visible"],
    }


class ControlActivatorTests(unittest.TestCase):
    def test_finds_unique_exact_and_substring_control(self):
        tree = node("App", "application", children=[
            node("Save", path="0/0"),
            node("Save As", path="0/1"),
        ])
        activator = ControlActivator(FakeService(tree))

        exact = activator.find(app_name="App", name="save")
        self.assertTrue(exact["success"], exact)
        self.assertEqual(exact["control"]["path"], "0/0")

        partial = activator.find(app_name="App", name="as")
        self.assertTrue(partial["success"], partial)
        self.assertEqual(partial["control"]["path"], "0/1")

    def test_role_filter_and_ambiguity(self):
        tree = node("App", "application", children=[
            node("Open", "push button", "0/0"),
            node("Open", "menu item", "0/1"),
        ])
        activator = ControlActivator(FakeService(tree))

        filtered = activator.find(
            app_name="App", name="open", role="menu item"
        )
        self.assertTrue(filtered["success"], filtered)
        self.assertEqual(filtered["control"]["path"], "0/1")

        ambiguous = activator.find(app_name="App", name="open")
        self.assertFalse(ambiguous["success"])
        self.assertEqual(ambiguous["code"], "ambiguous")
        self.assertEqual(ambiguous["count"], 2)

    def test_not_found_and_invalid_input(self):
        activator = ControlActivator(FakeService(node("App", "application")))

        missing = activator.find(app_name="App", name="save")
        self.assertEqual(missing["code"], "not_found")

        invalid = activator.find(app_name="", name="save")
        self.assertEqual(invalid["code"], "invalid_input")

        both = activator.find(process_id=1, app_name="App", name="save")
        self.assertEqual(both["code"], "invalid_input")

    def test_activation_requires_approval_before_any_service_action(self):
        tree = node("App", "application", children=[node("Save", path="0/0")])
        service = FakeService(tree)
        activator = ControlActivator(service)

        denied = activator.activate(app_name="App", name="Save")
        self.assertFalse(denied["success"])
        self.assertEqual(denied["code"], "approval_required")
        self.assertEqual(service.actions, [])

    def test_activation_uses_pid_and_stale_name_guard(self):
        tree = node("App", "application", children=[node("Save", path="0/0")])
        service = FakeService(tree, process_id=4321)
        service.action_result = {
            "success": True,
            "verification": {"status": "verified"},
        }
        activator = ControlActivator(service)

        result = activator.activate(
            app_name="App", name="Save", confirmed=True
        )

        self.assertTrue(result["success"], result)
        self.assertTrue(result["verified"])
        self.assertEqual(service.actions[0]["process_id"], 4321)
        self.assertEqual(service.actions[0]["path"], "0/0")
        self.assertEqual(service.actions[0]["action_name"], "press")
        self.assertEqual(service.actions[0]["expected_name"], "Save")
        self.assertTrue(service.actions[0]["confirmed"])

    def test_activation_with_explicit_pid(self):
        tree = node("App", "application", children=[node("Save", path="0/0")])
        service = FakeService(tree)
        activator = ControlActivator(service)

        result = activator.activate(
            process_id=99, name="Save", confirmed=True
        )

        self.assertTrue(result["success"], result)
        self.assertEqual(service.actions[0]["process_id"], 99)

    def test_action_failure_is_reported(self):
        tree = node("App", "application", children=[node("Save", path="0/0")])
        service = FakeService(tree)
        service.action_result = {"success": False}
        activator = ControlActivator(service)

        result = activator.activate(
            app_name="App", name="Save", confirmed=True
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "action_failed")

    def test_missing_application_pid_is_stale(self):
        tree = node("App", "application", children=[node("Save", path="0/0")])
        service = FakeService(tree, process_id=None)
        activator = ControlActivator(service)

        result = activator.activate(
            app_name="App", name="Save", confirmed=True
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "stale_target")


class StrictFakeService:
    """Fake AT-SPI service that enforces the real service's stale guard.

    ``inspect_application`` returns a deep copy of the live tree.
    ``perform_action`` resolves the control at ``path`` from the live tree
    and refuses when its current label differs from ``expected_name`` --
    exactly like ``AccessibilityService._resolve_action_target``.
    """

    def __init__(self, tree, process_id=4321):
        self.tree = tree
        self.process_id = process_id
        self.inspections = []
        self.actions = []
        self.presses = 0

    def _walk(self):
        stack = [self.tree]
        while stack:
            item = stack.pop()
            yield item
            stack.extend(reversed(item.get("children") or []))

    def inspect_application(self, **kwargs):
        self.inspections.append(kwargs)
        return {
            "success": True,
            "application": {"process_id": self.process_id, "name": "App"},
            "tree": copy.deepcopy(self.tree),
        }

    def perform_action(self, *, process_id, path, action_name,
                       confirmed=False, expected_name=None):
        self.actions.append({
            "process_id": process_id,
            "path": path,
            "action_name": action_name,
            "confirmed": confirmed,
            "expected_name": expected_name,
        })
        if confirmed is not True:
            return {"success": False, "requires_confirmation": True,
                    "message": "Press requires explicit confirmation."}
        current = next(
            (item for item in self._walk() if item.get("path") == path),
            None,
        )
        if current is None:
            return {"success": False, "message": "Control disappeared."}
        if expected_name is not None and current.get("name") != expected_name:
            return {"success": False,
                    "message": "Control name changed."}
        self.presses += 1
        return {"success": True,
                "verification": {"status": "unverified"}}


def fake_tree(label="Save"):
    return node(
        "App",
        "application",
        children=[node(label, path="0/0")],
    )


class RegistryActivationIntegrationTests(unittest.TestCase):
    """Real ToolRegistry -> real activator -> guard-enforcing fake service."""

    def make_registry(self, tree):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        service = StrictFakeService(tree)
        activator = ControlActivator(service)
        registry = ToolRegistry(
            executor=Mock(),
            memory_store=MemoryStore(Path(directory.name) / "memory.sqlite3"),
            control_activator=activator,
        )
        return registry, service

    def activate(self, registry, control_name, **extra):
        arguments = {"app_name": "App", "control_name": control_name}
        arguments.update(extra)
        return registry.execute(
            "accessibility_activate", arguments, confirmed=True
        )

    def test_case_insensitive_and_unique_substring_queries_activate(self):
        for query in ("Save", "save", "SAVE", "ave"):
            with self.subTest(query=query):
                registry, service = self.make_registry(fake_tree())
                result = self.activate(registry, query)
                self.assertTrue(result["success"], result)
                self.assertEqual(result["code"], "ok")
                self.assertEqual(service.presses, 1)
                # The stale guard sees the resolved label, never the query.
                self.assertEqual(service.actions[0]["expected_name"], "Save")
                self.assertEqual(result["control"]["name"], "Save")

    def test_role_filtered_lookup_activates(self):
        registry, service = self.make_registry(fake_tree())
        result = self.activate(registry, "ave", role="push button")
        self.assertTrue(result["success"], result)
        self.assertEqual(service.actions[0]["expected_name"], "Save")

    def test_ambiguous_query_is_never_pressed(self):
        tree = node("App", "application", children=[
            node("Save", path="0/0"),
            node("Save as", path="0/1"),
        ])
        registry, service = self.make_registry(tree)
        result = self.activate(registry, "sav")
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "ambiguous")
        self.assertEqual(service.actions, [])

    def test_rename_between_find_and_press_is_stale(self):
        tree = fake_tree("Save")
        service = StrictFakeService(tree)
        # The service resolves the control afresh by path, so renaming the
        # node before activation models a rename that landed between the
        # activator's lookup and the service's own re-resolution.
        original_inspect = service.inspect_application

        renamed = [False]

        def inspect_then_rename(**kwargs):
            result = original_inspect(**kwargs)
            if not renamed[0]:
                renamed[0] = True
                service.tree["children"][0]["name"] = "Save all"
            return result

        service.inspect_application = inspect_then_rename
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        registry = ToolRegistry(
            executor=Mock(),
            memory_store=MemoryStore(Path(directory.name) / "memory.sqlite3"),
            control_activator=ControlActivator(service),
        )
        result = self.activate(registry, "save")
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "stale_target")
        self.assertEqual(service.presses, 0)

    def test_approval_is_still_required(self):
        registry, service = self.make_registry(fake_tree())
        result = registry.execute(
            "accessibility_activate",
            {"app_name": "App", "control_name": "save"},
        )
        self.assertFalse(result["success"])
        self.assertEqual(service.actions, [])

    def test_legacy_adapter_delegates_to_the_live_path(self):
        tree = fake_tree()
        service = StrictFakeService(tree)
        adapter = legacy_actions.ControlActivator(service=service)
        self.assertTrue(adapter.DEPRECATED)

        found = adapter.find(4321, "ave")
        self.assertTrue(found["success"], found)
        self.assertEqual(found["target"]["name"], "Save")

        result = adapter.activate(4321, "ave", confirmed=True)
        self.assertTrue(result["success"], result)
        self.assertEqual(service.actions[0]["expected_name"], "Save")

        stale = adapter.activate(
            4321, "ave", confirmed=True, expected_name="Save all"
        )
        self.assertFalse(stale["success"])
        self.assertEqual(stale["code"], "stale_target")

    def test_registry_path_uses_live_activator(self):
        """AST check: production code constructs only the live adapter."""
        root = Path(inspect.getfile(ToolRegistry)).resolve().parents[1]
        offenders = []
        for path in sorted(root.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            names = _legacy_bindings(ast.parse(source))
            for item in ast.walk(ast.parse(source)):
                if _calls_legacy_activator(item, names):
                    offenders.append(f"{path.relative_to(root)}:{item.lineno}")
        self.assertEqual(offenders, [], "legacy ControlActivator constructed")
        # The detector itself must still recognize the deprecated path, or
        # this test would pass vacuously.
        probe = ast.parse(
            "import app.desktop.accessibility.actions as legacy\n"
            "legacy.ControlActivator(service=None)\n"
        )
        names = _legacy_bindings(probe)
        self.assertTrue(
            any(_calls_legacy_activator(item, names)
                for item in ast.walk(probe))
        )


LIVE_ACTIVATOR = "app.desktop.accessibility.activator"
LEGACY_ACTIVATOR = "app.desktop.accessibility.actions"


def _legacy_bindings(tree):
    """Names in one module that resolve to the deprecated adapter class.

    Covers ``from app.desktop.accessibility import actions`` (then
    ``actions.ControlActivator``), ``import app.desktop.accessibility.actions
    as legacy`` and ``from ...actions import ControlActivator as X``.
    """
    names = set()
    for item in ast.walk(tree):
        if isinstance(item, ast.Import):
            for alias in item.names:
                if alias.name == LEGACY_ACTIVATOR:
                    names.add(alias.asname or alias.name)
        elif isinstance(item, ast.ImportFrom) and not item.level:
            module = item.module or ""
            for alias in item.names:
                bound = alias.asname or alias.name
                if alias.name == "ControlActivator" and (
                    module == LEGACY_ACTIVATOR
                ):
                    names.add(bound)
                elif module + "." + alias.name == LEGACY_ACTIVATOR:
                    names.add(bound)
    return names


def _calls_legacy_activator(node, names):
    """True when ``node`` is a Call constructing the deprecated adapter."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        # ``ControlActivator(...)`` only counts when that name is bound to
        # the deprecated module in this file.
        return func.id in names
    if isinstance(func, ast.Attribute) and func.attr == "ControlActivator":
        owner = func.value
        return isinstance(owner, ast.Name) and owner.id in names
    return False


if __name__ == "__main__":
    unittest.main()
