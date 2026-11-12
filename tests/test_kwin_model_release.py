"""Standalone agent runs must also free Ministral before a visual model starts."""
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.brain.agent import MistyAgent
from app.brain.kwin_controller import KWinTaskController
from tests.test_kwin_visual_controller import FakeProvider


class DefaultVisualReleaseTests(unittest.TestCase):
    def test_standalone_agent_stops_its_model_before_visual_inference(self):
        agent = MistyAgent(registry=object())
        self.assertEqual(agent.before_visual_callback, agent.stop)

    def test_voice_controller_hook_still_wins_when_supplied(self):
        hook = Mock()
        agent = MistyAgent(registry=object(), before_visual_callback=hook)
        self.assertIs(agent.before_visual_callback, hook)

    def test_live_qwen_is_stopped_before_the_visual_model_starts(self):
        order = []
        registry = SimpleNamespace(input_backend=object())
        registry.resolve_window_target = Mock(
            return_value={"success": True, "window_id": "discord"})
        agent = SimpleNamespace(
            registry=registry,
            cancel_event=threading.Event(),
            _emit_activity=lambda event: None,
            confirmation_callback=lambda name, arguments, description: (
                order.append("approval") or name == "kwin_observe"),
        )
        agent.process = Mock(poll=lambda: None)  # Ministral resident.
        def stop():
            order.append("stop_qwen")
            agent.process = None
        agent.before_visual_callback = stop
        class Runtime:
            def start(self, **kwargs):
                order.append("visual_start")
            def decide(self, **kwargs):
                order.append("visual_decide")
                return {"kind": "ask", "question": "Which channel?"}
            def close(self):
                order.append("visual_close")
        controller = KWinTaskController(
            agent, provider_factory=lambda registry: FakeProvider(registry),
            runtime_factory=lambda: Runtime(), environment={"MISTY_KWIN_INPUT": "0"})
        result = controller.run("Click Dragon Cave in Discord")
        self.assertEqual(result.status, "ask")
        self.assertLess(order.index("stop_qwen"), order.index("visual_start"))
        self.assertLess(order.index("visual_start"), order.index("visual_decide"))


if __name__ == "__main__":
    unittest.main()
