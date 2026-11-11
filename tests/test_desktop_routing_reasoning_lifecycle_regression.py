"""Regression coverage for visual routing and Qwen transport lifecycle."""
import json
import unittest
from unittest.mock import Mock, patch

from app.brain.agent import MistyAgent
from app.brain.task_grant import looks_like_desktop_work, route_from_model


class DesktopRoutingLifecycleRegressionTests(unittest.TestCase):
    def test_explicit_screen_request_survives_classifier_failure(self):
        agent = Mock()
        agent._chat.side_effect = RuntimeError("model unavailable")
        self.assertTrue(looks_like_desktop_work(
            agent, "Click Dragon Cave on the right screen"
        ))
        self.assertEqual(agent._chat.call_count, 2)

    def test_nonvisual_request_does_not_gain_desktop_route_on_failure(self):
        agent = Mock()
        agent._chat.side_effect = RuntimeError("model unavailable")
        self.assertFalse(looks_like_desktop_work(agent, "what time is it"))
        agent._chat.assert_not_called()

    def test_malformed_classifier_uses_explicit_screen_fallback(self):
        agent = Mock()
        agent._chat.return_value = {"content": "not JSON"}
        self.assertTrue(looks_like_desktop_work(agent, "Click in Discord"))
        self.assertEqual(agent._chat.call_count, 2)

    def test_direct_classifier_cannot_hide_explicit_screen_request(self):
        agent = Mock()
        agent._chat.return_value = {"content": '{"route": "direct"}'}
        self.assertTrue(looks_like_desktop_work(agent, "Click the tray icon"))
        self.assertEqual(agent._chat.call_count, 1)

    @staticmethod
    def response(content):
        response = Mock()
        response.read.return_value = json.dumps({
            "choices": [{"message": {"content": content}}]
        }).encode()
        manager = Mock()
        manager.__enter__ = Mock(return_value=response)
        manager.__exit__ = Mock(return_value=False)
        return manager

    def test_routing_starts_reasoning_before_transport(self):
        agent = MistyAgent(registry=Mock())
        events = []
        agent.start = Mock(side_effect=lambda: events.append("ready"))
        def transport(*args, **kwargs):
            events.append("chat")
            return self.response('{"route": "desktop"}')
        with patch("app.brain.agent.urllib.request.urlopen", side_effect=transport):
            self.assertEqual(route_from_model(agent, "Click Dragon Cave"), "desktop")
        self.assertEqual(events, ["ready", "chat"])

    def test_every_post_vision_decision_reensures_reasoning(self):
        agent = MistyAgent(registry=Mock())
        state = {"loaded": False}
        events = []
        def start():
            if not state["loaded"]:
                events.append("load")
                state["loaded"] = True
        agent.start = Mock(side_effect=start)
        def transport(*args, **kwargs):
            self.assertTrue(state["loaded"])
            events.append("chat")
            return self.response('{"kind": "done"}')
        with patch("app.brain.agent.urllib.request.urlopen", side_effect=transport):
            agent._chat([])
            state["loaded"] = False  # vision's before_analyze unload hook
            events.append("vision_unload")
            agent._chat([])
            agent._chat([])  # running process stays loaded
        self.assertEqual(events, [
            "load", "chat", "vision_unload", "load", "chat", "chat"
        ])
        self.assertEqual(agent.start.call_count, 3)

    def test_startup_failure_never_sends_chat_request(self):
        agent = MistyAgent(registry=Mock())
        agent.start = Mock(side_effect=RuntimeError("startup failed"))
        with patch("app.brain.agent.urllib.request.urlopen") as transport:
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                agent._chat([])
        transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
