"""Escape owns the entire voice request, including pre-task audio/decoding."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.engine.voice_controller import VoiceController
from tests.test_typed_request_state import build_controller, FakeAgent
from tests.test_voice_confirmation_binding import GatedTranscriber
from tests.test_cancel_retry import ArgRegistry
from tests.agent_fakes import TestableMistyAgent, TRANSIENT_FAILURE, plan_response, plan_step


class VoiceRequestCancellationTests(unittest.TestCase):
    def make(self):
        controller = build_controller()
        controller._recording_generation = 0
        controller._task_agent = lambda: FakeAgent()
        controller._join_preload_before_transcribe = Mock()
        controller._handle_pending_window_selection = Mock(return_value=None)
        controller._typed_run_agent = Mock(return_value="done")
        controller._notify_escape_state = Mock()
        self.addCleanup(controller._confirmation_broker.cancel)
        return controller

    def test_cancel_held_recording_discards_and_release_cannot_start_worker(self):
        c = self.make()
        c._recording = True
        c._voice_cancel_event = threading.Event()
        token = c._voice_cancel_event
        c.recorder.discard = Mock()
        c.ready_reset_requested.emit = Mock()
        with patch("app.engine.voice_controller.threading.Thread") as thread:
            self.assertTrue(c.cancel_active_request())
            self.assertTrue(token.is_set())
            c.recorder.discard.assert_called_once()
            self.assertFalse(c._recording)
            self.assertTrue(c._busy)  # preload cleanup, not a new request
            c._release()
            self.assertEqual(thread.call_count, 1)
            self.assertEqual(thread.call_args.kwargs["name"], "misty-cancel-recording")
        c._busy = False

    def test_cancel_during_decode_drops_late_text_and_rejects_new_admission(self):
        c = self.make()
        decoder = GatedTranscriber()
        decoder.text = "close the wrong window"
        c._transcriber = decoder
        token = threading.Event()
        c._voice_cancel_event = token
        c._busy = True
        worker = threading.Thread(target=c._process_request, args=([0.1], token))
        c._worker = worker
        worker.start()
        try:
            self.assertTrue(decoder.entered.wait(2))
            self.assertIs(c._task_cancel_event, token)
            self.assertTrue(c.cancel_active_request())
            self.assertTrue(token.is_set())
            self.assertTrue(c._busy)
            self.assertFalse(c.submit_text("new request").accepted)
        finally:
            decoder.gate.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(c.transcript_changed.values, [])
        c._handle_pending_window_selection.assert_not_called()
        c._typed_run_agent.assert_not_called()
        self.assertFalse(c._busy)
        self.assertIsNone(c._voice_cancel_event)

    def test_cancel_before_worker_start_is_not_reset_by_session(self):
        c = self.make()
        token = threading.Event()
        token.set()
        c._voice_cancel_event = token
        c._transcriber = SimpleNamespace(transcribe=Mock())
        c._process_request([0.1], token)
        self.assertIs(c._task_cancel_event, token)
        c._transcriber.transcribe.assert_not_called()
        c._typed_run_agent.assert_not_called()

    def test_cancel_during_preload_wait_never_starts_decode(self):
        c = self.make()
        token = threading.Event()
        c._join_preload_before_transcribe.side_effect = token.set
        c._transcriber = SimpleNamespace(transcribe=Mock())
        c._voice_request_body([0.1], token)
        c._transcriber.transcribe.assert_not_called()
        c._handle_pending_window_selection.assert_not_called()

    def test_stale_binding_notification_uses_current_owner_not_old_finish(self):
        c = self.make()
        c.set_task_escape = Mock()
        c._recording = True
        c._busy = False
        from app.main import MistyApplication
        owner = SimpleNamespace(voice_controller=c, main_window=SimpleNamespace(task_card=None))
        MistyApplication._task_card_finished(owner, {"event_id": "old-task"})
        c.set_task_escape.assert_called_once_with(True)
        c._recording = False
        c.sync_task_escape()
        c.set_task_escape.assert_called_with(False)

    def test_cancel_confirmation_discards_held_answer_and_denies_pending(self):
        c = self.make()
        c._busy = True
        c._confirmation_recording = True
        pending = c._confirmation_broker.begin("close_window", {}, "Close?")
        c._confirmation_request_id = pending["request_id"]
        c._task_cancel_event = threading.Event()
        c.recorder.discard = Mock()
        self.assertTrue(c.cancel_active_request())
        self.assertTrue(c._task_cancel_event.is_set())
        self.assertFalse(c._confirmation_broker.is_pending())
        c.recorder.discard.assert_called_once()
        c._start_confirmation_worker = Mock()
        c._release()
        c._start_confirmation_worker.assert_not_called()

    def test_cancelled_owner_cannot_create_an_approval(self):
        c = self.make()
        c._task_cancel_event = threading.Event()
        c._task_cancel_event.set()
        with patch.object(c._confirmation_broker, "begin") as begin:
            self.assertFalse(c._request_confirmation("close_window", {}, "Close?"))
            begin.assert_not_called()

    def test_cancel_between_approval_checks_denies_without_wait(self):
        c = self.make()
        token = threading.Event()
        c._task_cancel_event = token
        begin = c._confirmation_broker.begin
        def racing_begin(*args):
            pending = begin(*args)
            token.set()
            return pending
        with patch.object(c._confirmation_broker, "begin", side_effect=racing_begin), \
             patch.object(c._confirmation_broker, "wait") as wait:
            self.assertFalse(c._request_confirmation("close_window", {}, "Close?"))
            wait.assert_not_called()
        self.assertFalse(c._confirmation_broker.is_pending())

    def test_cancelled_owner_button_can_only_deny(self):
        c = self.make()
        c._task_cancel_event = threading.Event()
        pending = c._confirmation_broker.begin("close_window", {}, "Close?")
        c._task_cancel_event.set()
        self.assertTrue(c.resolve_confirmation(True, pending["request_id"]))
        self.assertFalse(c._confirmation_broker.wait(pending))

    def test_approval_racing_cancel_does_not_report_approved(self):
        c = self.make()
        c._task_cancel_event = threading.Event()
        def racing_wait(pending):
            c._task_cancel_event.set()
            c._confirmation_broker.resolve(True, pending["request_id"])
            return True
        with patch.object(c._confirmation_broker, "wait", side_effect=racing_wait):
            self.assertFalse(c._request_confirmation("close_window", {}, "Close?"))

    def test_prompt_resolving_while_microphone_opens_discards_answer(self):
        c = self.make()
        c._busy = True
        pending = c._confirmation_broker.begin("close_window", {}, "Close?")
        c.recorder.start = Mock(side_effect=lambda: c._confirmation_broker.resolve(
            True, pending["request_id"]))
        c.recorder.discard = Mock()
        c._press()
        c.recorder.discard.assert_called_once()
        self.assertFalse(c._confirmation_recording)
        self.assertIsNone(c._confirmation_request_id)
        self.assertNotIn("listening", c.state_changed.values)
        c._start_confirmation_worker = Mock()
        c._release()
        c._start_confirmation_worker.assert_not_called()

    def test_agent_factory_failure_releases_admission_ownership(self):
        c = self.make()
        c._busy = True
        token = threading.Event()
        c._voice_cancel_event = token
        c._task_agent = Mock(side_effect=RuntimeError("factory failed"))
        c._cleanup_models = Mock()
        with self.assertRaisesRegex(RuntimeError, "factory failed"):
            c._process_request([0.1], token)
        self.assertFalse(c._busy)
        self.assertFalse(c._agent_busy)
        self.assertIsNone(c._voice_cancel_event)
        c._cleanup_models.assert_called_once()
        c._notify_escape_state.assert_called()


class ToolCancellationGateTests(unittest.TestCase):
    def test_cancel_after_approval_prevents_tool_execution(self):
        registry = ArgRegistry()
        registry.requires_confirmation = lambda *args: True
        agent = TestableMistyAgent(registry, [plan_response([plan_step("s1", "get_time")]), {"content": "done"}])
        agent.cancel_event = threading.Event()
        def approve(*args):
            agent.cancel_event.set()
            return True
        agent.confirmation_callback = approve
        agent.run("get the current time")
        self.assertEqual(registry.executed, [])

    def test_cancel_during_retry_delay_prevents_second_attempt(self):
        registry = ArgRegistry({"get_time": [dict(TRANSIENT_FAILURE)]})
        agent = TestableMistyAgent(registry, [])
        agent.cancel_event = threading.Event()
        with patch("app.brain.agent.time.sleep", side_effect=lambda _: agent.cancel_event.set()):
            result = agent._execute_with_single_retry("get_time", {}, confirmed=False)
        self.assertEqual(registry.executed, ["get_time"])
        self.assertTrue(result["cancelled"])


if __name__ == "__main__":
    unittest.main()
