"""Request-bound voice confirmations: audio may only decide its own request."""

import os
import threading
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.engine.confirmations import ConfirmationBroker  # noqa: E402
from app.engine.voice_controller import VoiceController  # noqa: E402


class SignalCollector:
    def __init__(self):
        self.values = []

    def emit(self, value=None):
        self.values.append(value)


class FakeRecorder:
    def __init__(self):
        self.started = 0
        self.audio = [0.1, 0.2]

    def start(self):
        self.started += 1

    def stop(self):
        return list(self.audio)

    def close(self):
        pass


class GatedTranscriber:
    """Transcriber whose transcribe() blocks until the test releases it."""

    def __init__(self):
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.text = "confirm"
        self.transcribed_audio = []

    def load(self):
        pass

    def transcribe(self, audio):
        self.transcribed_audio.append(audio)
        self.entered.set()
        if not self.gate.wait(5):
            raise RuntimeError("test gate never opened")
        return {"text": self.text, "language": "en"}

    def unload(self):
        pass


class TranscriberFactory:
    def __init__(self):
        self.created = []

    def __call__(self, *_args, **_kwargs):
        transcriber = GatedTranscriber()
        self.created.append(transcriber)
        return transcriber


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


NOOP_TIMER = lambda *args, **kwargs: None


def build_controller(factory, broker, clock=None):
    """Wire only the attributes the confirmation path touches."""
    controller = VoiceController.__new__(VoiceController)
    controller._shutting_down = False
    controller._recording = False
    controller._confirmation_recording = False
    controller._confirmation_request_id = None
    controller._confirmation_worker = None
    controller._recording_generation = 0
    controller._watchdog_timer = None
    controller.max_recording_seconds = 120
    controller._recording_lock = threading.Lock()
    controller._busy = False
    controller._transcriber = None
    controller._whisper_thread = None
    controller.recorder = FakeRecorder()
    controller.shortcut = "Ctrl+Alt+M"
    controller._confirmation_broker = broker
    controller._transcriber_factory = factory
    controller.response_changed = SignalCollector()
    controller.state_changed = SignalCollector()
    controller.confirmation_resolved = SignalCollector()
    controller.activity_event = SignalCollector()
    controller.completion_timer = None
    controller._completion_timer = type(
        "Timer", (), {"stop": NOOP_TIMER}
    )()
    controller._completion_waiting_for_hover = False
    return controller


class BrokerFixture:
    def __init__(self, timeout=60):
        self.clock = FakeClock()
        self.broker = ConfirmationBroker(timeout=timeout, clock=self.clock)


def join_worker(controller):
    worker = controller._confirmation_worker
    if worker is not None:
        worker.join(timeout=5)
        assert not worker.is_alive()


class VoiceConfirmationBindingTests(unittest.TestCase):
    def tearDown(self):
        if hasattr(self, "controller"):
            self.controller._finish_recording()

    def make(self, timeout=60):
        self.fixture = BrokerFixture(timeout=timeout)
        self.factory = TranscriberFactory()
        self.controller = build_controller(
            self.factory, self.fixture.broker
        )
        return self.controller

    def start_confirmation_recording(self):
        controller = self.controller
        controller._busy = True
        controller._press()
        self.assertTrue(controller._confirmation_recording)
        controller._confirmation_recording = False
        audio = controller.recorder.stop()
        return controller._start_confirmation_worker(
            audio, controller._confirmation_request_id
        )

    def test_recording_captures_the_pending_request_id(self):
        controller = self.make()
        first = self.fixture.broker.begin("close_window", {}, "Close A")

        controller._busy = True
        controller._press()

        self.assertEqual(
            controller._confirmation_request_id, first["request_id"]
        )
        self.assertEqual(controller.recorder.started, 1)
        self.assertEqual(controller.pending_confirmation_id(), first["request_id"])
        controller._confirmation_recording = False

    def test_no_recording_without_a_pending_confirmation(self):
        controller = self.make()

        controller._busy = True
        controller._press()

        self.assertFalse(controller._confirmation_recording)
        self.assertEqual(controller.recorder.started, 0)

    def test_late_audio_for_denied_request_cannot_approve_next_request(self):
        controller = self.make()
        broker = self.fixture.broker

        first = broker.begin("close_window", {}, "Close A")
        controller._busy = True
        controller._press()
        self.assertEqual(
            controller._confirmation_request_id, first["request_id"]
        )

        # The user denies A with the dialog button while audio is recorded.
        self.assertTrue(controller.resolve_confirmation(False, first["request_id"]))
        controller._confirmation_recording = False
        audio = controller.recorder.stop()

        # A new, distinct action B becomes pending.
        second = broker.begin("forget_memory", {}, "Forget B")
        self.assertIsNotNone(second)
        self.assertNotEqual(first["request_id"], second["request_id"])

        # Audio decoding for A finishes only now and says "confirm".
        controller._start_confirmation_worker(
            audio, controller._confirmation_request_id
        )
        transcriber = self.factory.created[-1]
        self.assertTrue(transcriber.entered.wait(5))
        transcriber.gate.set()
        join_worker(controller)

        self.assertTrue(broker.is_pending())
        self.assertEqual(
            broker.current_request_id(), second["request_id"]
        )
        self.assertFalse(second["event"].is_set())
        self.assertFalse(second["approved"])
        self.assertIn(
            "That confirmation request has already expired.",
            controller.response_changed.values,
        )
        self.assertNotIn(True, controller.confirmation_resolved.values)

        # B is still confirmable by its own ID only.
        self.assertFalse(broker.resolve(True, request_id=first["request_id"]))
        self.assertTrue(broker.resolve(True, request_id=second["request_id"]))

    def test_release_threads_the_captured_id_not_current_state(self):
        controller = self.make()
        broker = self.fixture.broker

        first = broker.begin("close_window", {}, "Close A")
        controller._busy = True
        controller._press()
        self.assertEqual(
            controller._confirmation_request_id, first["request_id"]
        )

        broker.cancel()
        second = broker.begin("forget_memory", {}, "Forget B")

        transcriber_text = "confirm"
        controller._release()
        transcriber = self.factory.created[-1]
        transcriber.text = transcriber_text
        self.assertTrue(transcriber.entered.wait(5))
        transcriber.gate.set()
        join_worker(controller)

        self.assertEqual(broker.current_request_id(), second["request_id"])
        self.assertTrue(broker.is_pending())

    def test_expired_request_late_confirm_returns_false(self):
        controller = self.make(timeout=1)
        broker = self.fixture.broker
        pending = broker.begin("close_window", {}, "Close")

        controller._busy = True
        controller._press()
        controller._confirmation_recording = False
        audio = controller.recorder.stop()

        self.fixture.clock.advance(2)
        controller._start_confirmation_worker(
            audio, controller._confirmation_request_id
        )
        transcriber = self.factory.created[-1]
        self.assertTrue(transcriber.entered.wait(5))
        transcriber.gate.set()
        join_worker(controller)

        self.assertFalse(pending["approved"])
        self.assertFalse(broker.is_pending())
        self.assertNotIn(True, controller.confirmation_resolved.values)
        self.assertFalse(
            broker.resolve(True, request_id=pending["request_id"])
        )

    def test_confirm_resolves_the_bound_request(self):
        controller = self.make()
        broker = self.fixture.broker
        pending = broker.begin("close_window", {}, "Close")

        controller._busy = True
        controller._press()
        controller._confirmation_recording = False
        audio = controller.recorder.stop()
        controller._start_confirmation_worker(
            audio, controller._confirmation_request_id
        )
        transcriber = self.factory.created[-1]
        self.assertTrue(transcriber.entered.wait(5))
        transcriber.gate.set()
        join_worker(controller)

        self.assertFalse(broker.is_pending())
        self.assertTrue(pending["approved"])
        self.assertEqual(controller.confirmation_resolved.values, [True])

    def test_cancel_word_denies_bound_request(self):
        controller = self.make()
        broker = self.fixture.broker
        pending = broker.begin("close_window", {}, "Close")

        controller._busy = True
        controller._press()
        controller._confirmation_recording = False
        audio = controller.recorder.stop()
        controller._start_confirmation_worker(
            audio, controller._confirmation_request_id
        )
        transcriber = self.factory.created[-1]
        transcriber.text = "no"
        self.assertTrue(transcriber.entered.wait(5))
        transcriber.gate.set()
        join_worker(controller)

        self.assertFalse(broker.is_pending())
        self.assertFalse(pending["approved"])
        self.assertEqual(controller.confirmation_resolved.values, [False])

    def test_resolve_confirmation_without_id_never_resolves(self):
        controller = self.make()
        broker = self.fixture.broker
        pending = broker.begin("close_window", {}, "Close")

        self.assertFalse(controller.resolve_confirmation(True, None))
        self.assertFalse(controller.resolve_confirmation(True, ""))
        self.assertFalse(controller.resolve_confirmation(False, "other"))
        self.assertTrue(broker.is_pending())
        self.assertFalse(pending["event"].is_set())
        self.assertEqual(controller.confirmation_resolved.values, [])

    def test_typed_path_creates_transcriber_lazily(self):
        controller = self.make()
        broker = self.fixture.broker

        # Typed requests never press the shortcut: no transcriber is loaded.
        self.assertIsNone(controller._transcriber)
        pending = broker.begin("close_window", {}, "Close")

        controller._busy = True
        controller._press()
        self.assertIsNone(controller._transcriber)
        audio = controller.recorder.stop()
        request_id = controller._confirmation_request_id
        controller._confirmation_recording = False

        controller._start_confirmation_worker(audio, request_id)
        self.assertTrue(self.factory.created)
        transcriber = self.factory.created[0]
        self.assertTrue(transcriber.entered.wait(5))
        transcriber.gate.set()
        join_worker(controller)

        self.assertIs(controller._transcriber, transcriber)
        self.assertEqual(transcriber.transcribed_audio, [audio])
        self.assertFalse(broker.is_pending())
        self.assertTrue(pending["approved"])

    def test_confirmation_falls_back_to_buttons_without_transcription(self):
        controller = self.make()
        broker = self.fixture.broker

        def broken_factory(*_args, **_kwargs):
            raise RuntimeError("no whisper")

        controller._transcriber_factory = broken_factory
        broker.begin("close_window", {}, "Close")

        controller._busy = True
        controller._press()
        audio = controller.recorder.stop()
        request_id = controller._confirmation_request_id
        controller._confirmation_recording = False

        controller._start_confirmation_worker(audio, request_id)
        join_worker(controller)

        self.assertTrue(
            any(
                "dialog buttons" in str(message)
                for message in controller.response_changed.values
            )
        )
        self.assertTrue(broker.is_pending())
        self.assertEqual(controller.confirmation_resolved.values, [])

    def test_empty_recording_does_not_need_transcription(self):
        controller = self.make()
        broker = self.fixture.broker
        broker.begin("close_window", {}, "Close")

        controller._start_confirmation_worker(None, "some-request-id")

        self.assertEqual(self.factory.created, [])
        self.assertIn(
            "I didn't hear a confirmation. Please try again.",
            controller.response_changed.values,
        )

    def test_missing_request_id_audio_is_discarded(self):
        controller = self.make()
        broker = self.fixture.broker
        pending = broker.begin("close_window", {}, "Close")

        controller._start_confirmation_worker([0.1], None)
        controller._start_confirmation_worker([0.1], "")

        self.assertEqual(self.factory.created, [])
        self.assertTrue(broker.is_pending())
        self.assertFalse(pending["event"].is_set())
        self.assertEqual(
            controller.confirmation_resolved.values, []
        )

    def test_shutdown_denial_needs_no_request_id(self):
        controller = self.make()
        pending = self.fixture.broker.begin("close_window", {}, "Close")

        self.assertTrue(controller._confirm_pending(False))
        self.assertFalse(pending["approved"])
        with self.assertRaises(ValueError):
            controller._confirm_pending(True)


class PopupConfirmationResponseTests(unittest.TestCase):
    def make_main(self):
        from app.main import MistyApplication

        app = MistyApplication.__new__(MistyApplication)

        class Controller:
            def __init__(self):
                self.calls = []

            def resolve_confirmation(self, approved, request_id):
                self.calls.append((approved, request_id))
                return True

        app.voice_controller = Controller()
        return app

    def test_response_without_request_id_is_ignored(self):
        app = self.make_main()
        for message in (
            {"type": "confirmation_response", "approved": True},
            {"type": "confirmation_response", "approved": True,
             "request_id": None},
            {"type": "confirmation_response", "approved": True,
             "request_id": ""},
            {"type": "confirmation_response", "approved": True,
             "request_id": "   "},
            {"type": "confirmation_response", "approved": True,
             "request_id": 42},
        ):
            with self.subTest(message=message):
                app._handle_popup_message(message)
        self.assertEqual(app.voice_controller.calls, [])

    def test_response_with_request_id_is_forwarded(self):
        app = self.make_main()
        app._handle_popup_message({
            "type": "confirmation_response",
            "approved": True,
            "request_id": "abc",
        })
        app._handle_popup_message({
            "type": "confirmation_response",
            "approved": False,
            "request_id": "abc",
        })
        self.assertEqual(
            app.voice_controller.calls, [(True, "abc"), (False, "abc")]
        )


if __name__ == "__main__":
    unittest.main()
