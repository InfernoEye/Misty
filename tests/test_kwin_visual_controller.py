"""Native controller integration uses fake windows, model and input only."""
from io import BytesIO
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from app.brain.kwin_controller import KWinTaskController
from app.brain.visual_decision import VisualDecisionTimeout
from app.desktop.ownership import native_task_active


class FakeProvider:
    def __init__(self, registry):
        self.calls = []
        self.observations = 0
        self.target = {"id":"discord", "pid":42, "title":"PRIVATE Discord", "screen":"DP-3",
                       "active":True,"minimized":False,"on_current_desktop":True,
                       "geometry":{"x":4640,"y":0,"width":1200,"height":1800}}
        self.context = {"windows":[self.target],"screens":[],"active_window":self.target}
        self.outcome = {"success":True,"verified":False,"not_executed":False,"code":"input_delivered"}
        stream = BytesIO()
        Image.new("RGB", (10, 10), "blue").save(stream, format="PNG")
        self.image = stream.getvalue()
    def discover(self):
        # Target scoping resolves the named app through the shared registry.
        return self.context
    def observe(self, window_id, **kwargs):
        self.observations += 1
        return SimpleNamespace(observation_id=f"obs{self.observations}", target=self.target,
            image=self.image, frame_id="private-frame", digest="digest",
            elements=[{"element_token":f"token{self.observations}","text":"Dragoncave",
                       "x":1,"y":2,"width":4,"height":4}])
    def click(self, observation_id, token, **kwargs):
        self.calls.append((observation_id, token, kwargs))
        return self.outcome


class ControllerTests(unittest.TestCase):
    def setUp(self):
        state = patch.multiple("app.desktop.ownership", _ACTIVE=False, _BLOCKED=False)
        state.start()
        self.addCleanup(state.stop)
        self.events = []
        self.order = []
        self.active_model = False
        self.decisions = []
        self.runtime_calls = []
        self.closed = 0
        self.denied = set()
        self.agent = SimpleNamespace(registry=SimpleNamespace(input_backend=object()),
                                     cancel_event=threading.Event(), process=SimpleNamespace(poll=lambda: None))
        self.agent.registry.resolve_screen_target = Mock(return_value={"success":True,"screen_name":"DP-3"})
        self.agent.registry.resolve_window_target = Mock(return_value={"success":True,"window_id":"discord"})
        self.agent._emit_activity = self.events.append
        def release():
            self.agent.process = None
            self.order.append("release_reasoning")
            return False  # Existing hook returns False if already unloaded.
        self.agent.before_visual_callback = release
        def approve(name, arguments, description):
            self.assertFalse(self.active_model)
            self.assertIsNone(self.agent.process)
            self.assertTrue(native_task_active())
            self.order.append(name)
            return name not in self.denied
        self.agent.confirmation_callback = Mock(side_effect=approve)
        self.provider = FakeProvider(self.agent.registry)

    def runtime_factory(self):
        parent = self
        class Runtime:
            def start(self, **kwargs):
                parent.active_model = True
                parent.order.append("model_start")
            def decide(self, **kwargs):
                parent.runtime_calls.append(kwargs)
                decision = parent.decisions.pop(0)
                if isinstance(decision, BaseException):
                    raise decision
                return decision(kwargs) if callable(decision) else decision
            def close(self):
                parent.active_model = False
                parent.closed += 1
                parent.order.append("model_close")
        return Runtime()

    @staticmethod
    def act(context):
        n = context["observation_id"].replace("obs", "")
        return {"kind":"act","tool":"kwin_click","observation_id":context["observation_id"],
                "arguments":{"element_token":"token"+n}}

    @staticmethod
    def finish(context):
        return {"kind":"finish","summary":"visible", "evidence_ids":[context["observation_id"]]}

    def run_task(self, *, enabled=False, goal="Click Dragon Cave in Discord on the right screen", **kwargs):
        controller = KWinTaskController(self.agent, provider_factory=lambda registry:self.provider,
            runtime_factory=self.runtime_factory, environment={"MISTY_KWIN_INPUT":"1" if enabled else "0"}, **kwargs)
        result = controller.run(goal)
        if result.status in {"cleanup_failed", "input_outcome_unknown", "admission_blocked"}:
            self.assertTrue(native_task_active())
        else:
            self.assertFalse(native_task_active())
        return result

    def test_default_preview_sends_real_image_and_schemas_but_no_input(self):
        self.decisions = [self.act]
        result = self.run_task()
        self.assertEqual(result.status, "preview")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.closed, 1)
        call = self.runtime_calls[0]
        self.assertEqual(call["images"][0].data, self.provider.image)
        schema = call["tool_schemas"][0]["inputSchema"]
        self.assertEqual(set(schema["properties"]), {"element_token"})
        self.assertNotIn("PRIVATE", str(self.events))

    def test_input_requires_separate_approval_after_model_cleanup_then_reobserves(self):
        self.decisions = [self.act, self.finish]
        result = self.run_task(enabled=True)
        self.assertEqual(result.status, "model_observed")
        self.assertEqual(result.actions, 1)
        self.assertEqual(self.provider.observations, 2)
        self.assertEqual(self.provider.calls[0][0:2], ("obs1", "token1"))
        self.assertIs(self.provider.calls[0][2]["approved"], True)
        self.assertLess(self.order.index("model_close"), self.order.index("kwin_click"))
        self.assertIn("not independently verified", result.summary())
        self.agent.registry.resolve_screen_target.assert_called_once_with("right")
        self.agent.registry.resolve_window_target.assert_called_once_with("Discord", allow_fuzzy=True)

    def test_denied_observation_never_loads_model_or_captures(self):
        self.denied.add("kwin_observe")
        result = self.run_task(enabled=True)
        self.assertEqual(result.status, "denied")
        self.assertEqual(self.runtime_calls, [])
        self.assertEqual(self.provider.observations, 0)

    def test_denied_click_never_dispatches(self):
        self.denied.add("kwin_click")
        self.decisions = [self.act]
        self.assertEqual(self.run_task(enabled=True).status, "denied")
        self.assertEqual(self.provider.calls, [])

    def test_model_cannot_supply_coordinates_or_approval(self):
        for arguments in ({"x":1,"y":2}, {"element_token":"token1","approved":True}):
            with self.subTest(arguments=arguments):
                self.decisions = [{"kind":"act","tool":"kwin_click","observation_id":"obs1","arguments":arguments}]
                self.provider.observations = 0
                self.assertEqual(self.run_task(enabled=True).status, "invalid_decision")
                self.assertEqual(self.provider.calls, [])

    def test_uncertain_input_stops_without_replay(self):
        self.provider.outcome = {"success":False,"not_executed":False,"code":"input_outcome_unknown"}
        self.decisions = [self.act]
        self.assertEqual(self.run_task(enabled=True).status, "input_outcome_unknown")
        self.assertEqual(len(self.provider.calls), 1)

    def test_repeating_same_delivered_target_is_refused(self):
        self.decisions = [self.act, self.act]
        result = self.run_task(enabled=True)
        self.assertEqual(result.status, "repeat_refused")
        self.assertEqual(len(self.provider.calls), 1)

    def test_finish_needs_latest_observation_not_stale_or_string(self):
        for evidence in (["old"], "obs1"):
            with self.subTest(evidence=evidence):
                self.provider.observations = 0
                self.decisions = [{"kind":"finish","summary":"claimed", "evidence_ids":evidence}]
                self.assertEqual(self.run_task().status, "unverified_finish")

    def test_sensitive_request_is_not_observed(self):
        result = self.run_task(enabled=True, goal="Click buy and pay")
        self.assertEqual(result.status, "sensitive_action")
        self.assertEqual(self.provider.observations, 0)
        self.agent.confirmation_callback.assert_not_called()

    def test_cancel_before_task_does_not_observe_or_approve(self):
        self.agent.cancel_event.set()
        self.assertEqual(self.run_task().status, "canceled")
        self.assertEqual(self.provider.observations, 0)
        self.agent.confirmation_callback.assert_not_called()

    def test_visual_timeout_closes_before_return(self):
        self.decisions = [VisualDecisionTimeout("private detail")]
        self.assertEqual(self.run_task().status, "timeout")
        self.assertFalse(self.active_model)
        self.assertEqual(self.closed, 1)
        self.assertEqual(self.provider.calls, [])

    def test_cleanup_failure_prevents_approval_and_input(self):
        original = self.runtime_factory
        def broken_factory():
            runtime = original()
            runtime.close = Mock(side_effect=RuntimeError("private"))
            return runtime
        self.runtime_factory = broken_factory
        self.decisions = [self.act]
        result = self.run_task(enabled=True)
        self.assertEqual(result.status, "cleanup_failed")
        self.assertEqual(self.provider.calls, [])
        self.assertNotIn("kwin_click", self.order)

    def test_reasoning_release_failure_admits_no_model_or_approval(self):
        self.agent.before_visual_callback = lambda: None
        self.assertEqual(self.run_task().status, "reasoning_busy")
        self.assertEqual(self.provider.observations, 0)
        self.agent.confirmation_callback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
