"""The desktop task path is reachable from a real request.

A spoken task like "open Amazon and find me a good vacuum" must take the
observe → act → check loop, while ordinary requests keep using the one-shot
planner. These tests pin the routing gates and the grant behaviour, with the
model, the screen and the mouse all faked.
"""

import json
import pathlib
import tempfile
import unittest

from app.brain.task_grant import TaskGrant
from app.brain.tools import ToolRegistry
from app.brain.memory import MemoryStore
from tests.agent_fakes import TestableMistyAgent


class StubInput:
    def __init__(self):
        self.kinds = []

    def execute(self, action):
        self.kinds.append(action.kind)
        return {"success": True, "message": "ok"}


class FakeVision:
    """Reports one search box and a result count."""

    def __init__(self, text="Search products   Results: 1 item"):
        self.text = text
        self.capture_service = self

    def resolve_input_point(self, frame_id, x, y):
        # Fake a trusted 1:1 capture resolver; real transforms have separate tests.
        if frame_id != "test-frame":
            raise ValueError("Unknown test frame")
        return x, y

    def inspect(self, **kwargs):
        text = self.text

        class Vision:
            class ocr:
                pass

            visual = None
            frame = {"frame_id": "test-frame", "grounded": True}

        Vision.ocr.text = text
        Vision.image_size = (1920, 1080)
        return Vision()



class ChangingVision(FakeVision):
    """A screen that visibly changes each time it is observed.

    A static screen makes the loop stop (correctly), so a multi-action test
    needs an observation that actually progresses.
    """

    def __init__(self):
        super().__init__()
        self.count = 0

    def inspect(self, **kwargs):
        self.count += 1
        return _changed(f"Results page {self.count}")


def _changed(text):
    class Vision:
        image_size = (1920, 1080)
        visual = None
        frame = {"frame_id": "test-frame", "grounded": True}

        class ocr:
            pass

    Vision.ocr.text = text
    return Vision()


def build(**kwargs):
    tmp = pathlib.Path(tempfile.mkdtemp())
    return ToolRegistry(
        memory_store=MemoryStore(tmp / "memory.sqlite3"), **kwargs
    )


class RouteAgent(TestableMistyAgent):
    """Scripted model: answers the route question, then the loop rounds."""

    def __init__(self, registry, route, decisions):
        super().__init__(registry, [])
        self.route = route
        self.decisions = list(decisions)

    def _chat(self, messages, tools=None, response_format=None):
        system = messages[0]["content"] if messages else ""
        if isinstance(system, str) and "route" in system.casefold():
            if "desktop" in system.casefold() and "direct" in system.casefold():
                return {"content": json.dumps({"route": self.route})}
        if self.decisions:
            return {"content": json.dumps(self.decisions.pop(0))}
        return {"content": json.dumps({"kind": "done", "note": "finished"})}


def click(x=10, y=20, label=None):
    arguments = {"x": x, "y": y}
    if label:
        arguments["label"] = label
    return {"kind": "act", "tool": "desktop_click", "arguments": arguments}


class GateTests(unittest.TestCase):
    def test_without_an_input_backend_the_planner_is_used(self):
        registry = build()
        agent = RouteAgent(registry, "desktop", [click()])
        self.assertIsNone(
            agent._maybe_run_desktop_task("open amazon and search")
        )

    def test_without_vision_the_planner_is_used(self):
        registry = build(input_backend=StubInput())
        agent = RouteAgent(registry, "desktop", [click()])
        self.assertIsNone(
            agent._maybe_run_desktop_task("open amazon and search")
        )

    def test_a_direct_route_is_left_to_the_planner(self):
        registry = build(input_backend=StubInput(), vision_service=FakeVision())
        agent = RouteAgent(registry, "direct", [click()])
        self.assertIsNone(agent._maybe_run_desktop_task("what time is it"))

    def test_a_declined_task_touches_nothing(self):
        backend = StubInput()
        registry = build(input_backend=backend, vision_service=FakeVision())
        agent = RouteAgent(registry, "desktop", [click()])
        agent.confirmation_callback = lambda *args: False

        answer = agent._maybe_run_desktop_task("open amazon and search")

        self.assertIn("not approved", answer)
        self.assertEqual(backend.kinds, [])

    def test_a_missing_callback_counts_as_declined(self):
        backend = StubInput()
        registry = build(input_backend=backend, vision_service=FakeVision())
        agent = RouteAgent(registry, "desktop", [click()])
        agent.confirmation_callback = None

        answer = agent._maybe_run_desktop_task("open amazon and search")

        self.assertIn("not approved", answer)
        self.assertEqual(backend.kinds, [])


class LoopTests(unittest.TestCase):
    def test_an_approved_task_runs_the_loop(self):
        backend = StubInput()
        registry = build(input_backend=backend, vision_service=FakeVision())
        agent = RouteAgent(registry, "desktop", [
            click(),
            {"kind": "done", "note": "Search box focused."},
        ])
        agent.confirmation_callback = lambda *args: True

        answer = agent._maybe_run_desktop_task("open amazon and search")

        self.assertIn("Search box focused", answer)
        self.assertIn("1 action", answer)
        self.assertEqual(backend.kinds, ["pointer_click"])

    def test_the_grant_covers_the_task_and_then_ends(self):
        backend = StubInput()
        registry = build(input_backend=backend, vision_service=FakeVision())
        agent = RouteAgent(registry, "desktop", [
            click(), click(), {"kind": "done"},
        ])
        agent.confirmation_callback = lambda *args: True

        agent._maybe_run_desktop_task("open amazon and search")

        self.assertEqual(backend.kinds, ["pointer_click", "pointer_click"])
        # The grant must not outlive the task.
        self.assertIsNone(getattr(registry, "task_grant", None))
        self.assertEqual(
            registry.execute("desktop_click", {"x": 1, "y": 1}).get("code"),
            "confirmation_required",
        )

    def test_the_whole_task_asks_exactly_once(self):
        # Asking per click is what makes a 20-step task unusable; the grant
        # must cover every action inside the task.
        backend = StubInput()
        registry = build(input_backend=backend, vision_service=ChangingVision())
        agent = RouteAgent(registry, "desktop", [
            click(), click(), click(), {"kind": "done"},
        ])
        prompts = []

        def approve(tool, arguments, label):
            prompts.append(tool)
            return True

        agent.confirmation_callback = approve
        agent._maybe_run_desktop_task("open amazon and search")

        self.assertEqual(prompts, ["desktop_task"])
        self.assertEqual(
            backend.kinds, ["pointer_click"] * 3
        )

    def test_a_purchase_click_is_refused_even_inside_a_task(self):
        backend = StubInput()
        registry = build(input_backend=backend, vision_service=FakeVision())
        agent = RouteAgent(registry, "desktop", [
            click(label="Buy now"),
            {"kind": "done"},
        ])
        agent.confirmation_callback = lambda *args: True

        agent._maybe_run_desktop_task("open amazon and search")

        self.assertEqual(backend.kinds, [])

    def test_a_loop_error_does_not_escape(self):
        backend = StubInput()
        registry = build(input_backend=backend, vision_service=FakeVision())
        agent = RouteAgent(registry, "desktop", [])
        agent.confirmation_callback = lambda *args: True

        class Broken(FakeVision):
            def inspect(self, **kwargs):
                raise RuntimeError("capture failed")

        registry.vision_service = Broken()
        answer = agent._maybe_run_desktop_task("open amazon and search")
        self.assertIsInstance(answer, str)


class GrantScopeTests(unittest.TestCase):
    def test_a_grant_permits_repeated_desktop_actions(self):
        backend = StubInput()
        registry = build(input_backend=backend)
        with TaskGrant("task") as grant:
            registry.task_grant = grant
            for _ in range(3):
                self.assertEqual(
                    registry.execute("desktop_click", {"x": 1, "y": 1}).get("code"),
                    "done",
                )
        registry.task_grant = None
        self.assertEqual(
            registry.execute("desktop_click", {"x": 1, "y": 1}).get("code"),
            "confirmation_required",
        )

    def test_a_grant_does_not_allow_typing_outside_the_task(self):
        backend = StubInput()
        registry = build(input_backend=backend)
        self.assertEqual(
            registry.execute(
                "desktop_type_text", {"text": "hi"}
            ).get("code"),
            "confirmation_required",
        )
        self.assertEqual(backend.kinds, [])


if __name__ == "__main__":
    unittest.main()
