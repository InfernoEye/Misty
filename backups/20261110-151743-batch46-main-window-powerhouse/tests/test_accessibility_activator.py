import unittest

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


if __name__ == "__main__":
    unittest.main()
