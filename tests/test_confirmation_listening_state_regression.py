"""Voice approval must end listening on release, errors and task resumption."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.engine.confirmations import ConfirmationBroker
from tests.test_voice_confirmation_binding import build_controller, SignalCollector


class ConfirmationListeningStateTests(unittest.TestCase):
    def setUp(self):
        self.broker = ConfirmationBroker(timeout=0.1)
        self.controller = build_controller(lambda: None, self.broker)
        self.controller._busy = True
        self.controller.confirmation_requested = SignalCollector()
        self.controller.ready_reset_requested = SignalCollector()
        self.controller._has_pending_window_selection = lambda: False
        self.controller.recorder.discard = Mock()

    def tearDown(self):
        self.controller._finish_recording()
        self.broker.cancel()

    def pending(self):
        return self.broker.begin("desktop_task", {"task": "click target"}, "Operate desktop?")

    def test_release_ends_listening_and_cancels_watchdog(self):
        pending = self.pending()
        with patch("app.engine.voice_controller.threading.Timer") as timer:
            self.controller._press()
            timer.return_value.start.assert_called_once()
            self.assertEqual(self.controller.state_changed.values[-1], "listening")
            self.controller._start_confirmation_worker = Mock()
            self.controller._release()
            timer.return_value.cancel.assert_called_once()
        self.assertFalse(self.controller._confirmation_recording)
        self.assertIsNone(self.controller._watchdog_timer)
        self.assertEqual(self.controller.state_changed.values[-1], "transcribing")
        self.controller._start_confirmation_worker.assert_called_once_with([0.1, 0.2], pending["request_id"])

    def test_unanswered_decode_paths_restore_confirmation(self):
        for outcome in (None, "unclear", "error", "no-transcriber"):
            with self.subTest(outcome=outcome):
                self.broker.cancel()
                pending = self.pending()
                self.controller.state_changed.values.clear()
                transcriber = SimpleNamespace(transcribe=Mock(return_value={"text": outcome}))
                if outcome == "error":
                    transcriber.transcribe.side_effect = RuntimeError("decode failed")
                self.controller._obtain_confirmation_transcriber = lambda: (
                    None if outcome == "no-transcriber" else transcriber)
                audio = None if outcome is None else [0.1]
                self.controller._process_confirmation_audio(audio, pending["request_id"])
                self.assertEqual(self.controller.state_changed.values, ["confirmation"])
                self.assertTrue(self.broker.is_pending())

    def test_resolved_or_stale_decoders_never_publish_late_state(self):
        for answer in ("confirm", "cancel"):
            with self.subTest(answer=answer):
                pending = self.pending()
                self.controller.state_changed.values.clear()
                self.controller._obtain_confirmation_transcriber = lambda: SimpleNamespace(
                    transcribe=lambda audio: {"text": answer})
                self.controller._process_confirmation_audio([0.1], pending["request_id"])
                self.assertEqual(self.controller.state_changed.values, [])
                self.assertFalse(self.broker.is_pending())
        old = self.pending()
        self.broker.cancel()
        new = self.pending()
        self.controller.state_changed.values.clear()
        self.controller._process_confirmation_audio([0.1], old["request_id"])
        self.assertEqual(self.controller.state_changed.values, [])
        self.assertEqual(self.broker.current_request_id(), new["request_id"])

    def test_watchdog_restores_confirmation_without_idle_reset(self):
        self.pending()
        with patch("app.engine.voice_controller.threading.Timer"):
            self.controller._press()
        self.controller._recording_watchdog(self.controller._recording_generation)
        self.assertFalse(self.controller._confirmation_recording)
        self.assertEqual(self.controller.state_changed.values[-1], "confirmation")
        self.assertEqual(self.controller.ready_reset_requested.values, [])
        self.assertTrue(self.broker.is_pending())

    def test_owner_wait_resumes_thinking_after_answer_or_timeout(self):
        for approved in (True, False, None):
            with self.subTest(approved=approved):
                self.controller.state_changed.values.clear()
                announced = threading.Event()
                self.controller.confirmation_requested = SimpleNamespace(emit=lambda request: announced.set())
                result = []
                worker = threading.Thread(target=lambda: result.append(self.controller._request_confirmation(
                    "desktop_task", {}, "Operate desktop?")))
                worker.start()
                self.assertTrue(announced.wait(1))
                if approved is not None:
                    self.controller.resolve_confirmation(approved, self.broker.current_request_id())
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive())
                self.assertEqual(result, [approved is True])
                self.assertEqual(self.controller.state_changed.values, ["confirmation", "thinking"])

    def test_prompt_resolution_closes_a_still_held_recording(self):
        for approved in (True, False, None):
            with self.subTest(approved=approved):
                announced = threading.Event()
                self.controller.confirmation_requested = SimpleNamespace(
                    emit=lambda request: announced.set())
                worker = threading.Thread(target=lambda: self.controller._request_confirmation(
                    "desktop_task", {}, "Operate desktop?"))
                worker.start()
                self.assertTrue(announced.wait(1))
                with patch("app.engine.voice_controller.threading.Timer"):
                    self.controller._press()
                self.assertTrue(self.controller._confirmation_recording)
                if approved is not None:
                    self.controller.resolve_confirmation(approved, self.broker.current_request_id())
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive())
                self.assertFalse(self.controller._confirmation_recording)
                self.assertIsNone(self.controller._watchdog_timer)
                self.assertIsNone(self.controller._confirmation_request_id)
                self.assertEqual(self.controller.state_changed.values[-1], "thinking")

    def test_overlapping_confirmation_decoding_does_not_restart_recording(self):
        self.pending()
        self.controller._confirmation_worker = SimpleNamespace(is_alive=lambda: True)
        self.controller._press()
        self.assertEqual(self.controller.recorder.started, 0)


if __name__ == "__main__":
    unittest.main()
