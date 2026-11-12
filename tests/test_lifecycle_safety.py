"""Lifecycle safety: recording aborts, shutdown ownership, model exclusivity.

Everything here runs against fakes: no microphone, no models, no helper
process and no KDE. Qt objects (signals/QProcess plumbing) stay offscreen.
"""

import os
import subprocess
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402

import app.speech.recorder as recorder_module  # noqa: E402
from app.brain.session_context import SessionContext  # noqa: E402
from app.engine.confirmations import ConfirmationBroker  # noqa: E402
from app.engine.voice_controller import (  # noqa: E402
    _NO_SESSION_TURN,
    VoiceController,
)
from app.speech.recorder import MAX_QUEUED_BLOCKS, Recorder  # noqa: E402


class SignalCollector:
    def __init__(self):
        self.values = []

    def emit(self, *values):
        if len(values) == 1:
            values = values[0]
        self.values.append(values)


class FakeRecorder:
    """Recording double tracking start/stop/discard."""

    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.discarded = 0
        self.closed = 0
        self.active = False

    def prepare(self):
        pass

    def start(self):
        self.started += 1
        self.active = True

    def stop(self):
        self.stopped += 1
        self.active = False
        return [0.1, 0.2]

    def discard(self):
        self.discarded += 1
        self.active = False
        return True

    def close(self):
        self.closed += 1
        self.active = False


class OrderTranscriber:
    def __init__(self, order):
        self.order = order
        self.ready = False
        self.text = "open the browser"

    def load(self):
        self.ready = True
        self.order.append("whisper-load")

    def transcribe(self, audio):
        self.order.append("whisper-transcribe")
        return {"text": self.text, "language": "en"}

    def unload(self):
        self.order.append("whisper-unload")


class BlockingAgent:
    """Agent double whose start() blocks until its gate opens (Qwen preload)."""

    def __init__(self, order, gate=None):
        self.order = order
        self.gate = gate if gate is not None else threading.Event()
        self.process = None
        self.cancel_event = threading.Event()
        self.start_calls = 0
        self.stop_calls = 0

    def start(self):
        self.start_calls += 1
        if not self.gate.wait(10):
            raise RuntimeError("agent gate never opened")
        self.process = object()
        self.order.append("qwen-load")

    def run(self, transcript, **kwargs):
        self.order.append("qwen-run")
        return "done"

    def stop(self):
        self.stop_calls += 1
        self.order.append("qwen-stop")
        self.process = None


class FakeWindowService:
    def __init__(self):
        self.cleared = 0

    def capture_active_window(self):
        return {"success": False, "message": "no window"}

    def clear_active_window(self):
        self.cleared += 1


class FakeFileTranscriber:
    def __init__(self, order):
        self.order = order
        self.hook = None
        self.closed = 0

    def set_before_transcribe(self, callback):
        self.hook = callback

    def transcribe(self, folder, relative_path, language="auto"):
        if self.hook is not None:
            self.hook()
        self.order.append("file-whisper")
        return {"success": True, "text": "hello"}

    def close(self):
        self.closed += 1


class FakeHelperProcess:
    """Stands in for the QProcess so shutdown/rebind never spawns anything."""

    def __init__(self):
        self.terminated = 0
        self.killed = 0

    def state(self):
        return 2  # QProcess.NotRunning

    def errorString(self):
        return "helper unavailable"

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1

    def waitForFinished(self, _ms):
        return True

    def waitForStarted(self, _ms):
        return True


NOOP_TIMER = type("Timer", (), {
    "stop": lambda self: None,
    "start": lambda self, _ms: None,
})()


def bare_controller(order=None, agent=None, transcriber=None,
                    file_transcriber=None, max_recording_seconds=120.0):
    """Wire exactly the attributes the lifecycle paths touch."""
    order = order if order is not None else []
    controller = VoiceController.__new__(VoiceController)
    controller._shutting_down = False
    controller._recording = False
    controller._confirmation_recording = False
    controller._confirmation_request_id = None
    controller._confirmation_worker = None
    controller._busy = False
    controller._worker = None
    controller._start_error_thread = None
    controller._recording_generation = 0
    controller._recording_lock = threading.RLock()
    controller._watchdog_timer = None
    controller.max_recording_seconds = max_recording_seconds
    controller._cleanup_lock = threading.RLock()
    controller._agent = agent
    controller._agent_thread = None
    controller._agent_errors = []
    controller._agent_busy = False
    controller._preload_epoch = 0
    controller._whisper_thread = None
    controller._transcriber = transcriber
    controller._transcriber_factory = lambda: OrderTranscriber(order)
    controller.file_transcriber = file_transcriber
    controller.recorder = FakeRecorder()
    controller.window_service = FakeWindowService()
    controller.shortcut = "Ctrl+Alt+M"
    controller.process = FakeHelperProcess()
    controller.popup_ipc = type("Ipc", (), {"send": lambda self, m: True})()
    controller._confirmation_broker = ConfirmationBroker(timeout=30)
    controller.response_changed = SignalCollector()
    controller.state_changed = SignalCollector()
    controller.activity_event = SignalCollector()
    controller.confirmation_resolved = SignalCollector()
    controller.ready_reset_requested = SignalCollector()
    controller.window_selection_cleared = SignalCollector()
    controller.window_selection_message_requested = SignalCollector()
    controller.window_selection_requested = SignalCollector()
    controller._pending_window_selection = None
    controller._window_selection_lock = threading.Lock()
    controller._pending_window_selection_timer = None
    controller.popup_hovered = False
    controller._completion_timer = NOOP_TIMER
    controller._completion_waiting_for_hover = False
    controller._pending_clarification = None
    controller._typed_outcomes = []
    controller._shortcut_suspended = False
    controller._restarting_helper = False
    controller._helper_ready = True
    controller._shortcut_active = controller.shortcut
    controller._shortcut_apply_lock = threading.RLock()
    controller._shortcut_saved = controller.shortcut
    controller._helper_ready_event = threading.Event()
    controller._helper_error_reason = ""
    # Turn cancellation / task card / session states (newer turn plumbing).
    controller._task_lock = threading.RLock()
    controller._task_request = ""
    controller._task_event = None
    controller._task_cancel_event = None
    controller._task_failed_step = None
    controller.task_started = SignalCollector()
    controller._session_turn = _NO_SESSION_TURN
    controller._session_events = []
    controller._session_events_lock = threading.Lock()
    controller.session_context = SessionContext()
    controller._clarification_lock = threading.RLock()
    controller._clarification_flag = threading.Event()
    return controller


def start_recording(controller):
    controller.recorder.start()
    controller._recording = True
    controller._acquire_recording()


class RecordingAbortTests(unittest.TestCase):
    """A. Helper loss, rebind or watchdog must never leave the mic open."""

    def test_helper_finished_during_hold_aborts_recording(self):
        controller = bare_controller()
        start_recording(controller)

        controller._helper_finished(1, None)

        self.assertFalse(controller._recording)
        self.assertEqual(controller.recorder.discarded, 1)
        self.assertEqual(controller.recorder.stopped, 0)
        self.assertIsNone(controller._watchdog_timer)
        # One abort notification (the pre-existing helper-loss error does
        # still report itself separately, as before).
        aborts = [
            message for message in controller.response_changed.values
            if "Recording stopped" in message
        ]
        self.assertEqual(len(aborts), 1)
        self.assertEqual(controller.state_changed.values[0], "error")
        # No second shortcut is involved; a late RELEASE does nothing.
        controller._release()
        self.assertEqual(controller.recorder.stopped, 0)
        self.assertFalse(controller.has_live_work())

    def test_helper_error_during_hold_aborts_recording(self):
        controller = bare_controller()
        start_recording(controller)

        controller._helper_error(None)

        self.assertFalse(controller._recording)
        self.assertEqual(controller.recorder.discarded, 1)

    def test_abort_is_idempotent_and_emits_once(self):
        controller = bare_controller()
        start_recording(controller)

        self.assertTrue(controller._abort_recording("first reason"))
        self.assertFalse(controller._abort_recording("second reason"))

        self.assertEqual(controller.state_changed.values, ["error"])
        self.assertEqual(len(controller.response_changed.values), 1)
        self.assertIn("first reason", controller.response_changed.values[0])
        self.assertEqual(controller.recorder.discarded, 1)

    def test_rebind_during_hold_aborts_before_restart(self):
        controller = bare_controller()
        start_recording(controller)
        seen = []

        def fake_restart(sequence):
            seen.append(
                (sequence, controller._recording, controller.recorder.active,
                 controller.recorder.discarded)
            )
            controller._helper_ready_event = threading.Event()
            controller._helper_ready_event.set()

        controller._shortcut_restart = fake_restart

        self.assertTrue(controller.set_shortcut("Ctrl+Alt+K"))

        self.assertEqual(seen, [("Ctrl+Alt+K", False, False, 1)])
        self.assertEqual(controller.recorder.discarded, 1)
        self.assertEqual(controller.shortcut, "Ctrl+Alt+K")

    def test_suspend_during_hold_aborts(self):
        controller = bare_controller()
        start_recording(controller)

        controller.suspend_shortcut()

        self.assertFalse(controller._recording)
        self.assertEqual(controller.recorder.discarded, 1)

    def test_watchdog_aborts_with_short_injected_timeout(self):
        controller = bare_controller(max_recording_seconds=0.15)
        start_recording(controller)

        deadline = time.monotonic() + 3
        while controller._recording and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertFalse(controller._recording)
        self.assertEqual(controller.recorder.discarded, 1)
        self.assertTrue(
            any("seconds" in message
                for message in controller.response_changed.values)
        )
        self.assertFalse(controller.has_live_work())

    def test_stale_watchdog_generation_does_not_abort_new_recording(self):
        controller = bare_controller(max_recording_seconds=0.1)
        start_recording(controller)
        old_generation = controller._recording_generation
        controller._finish_recording()  # released
        controller._release = lambda: None
        start_recording(controller)  # a new hold owns a new generation

        controller._recording_watchdog(old_generation)
        self.assertTrue(controller._recording)

    def test_release_cancels_watchdog_and_keeps_audio(self):
        controller = bare_controller(max_recording_seconds=0.2)
        # This fixture owns recording state, not a complete request registry.
        # Test the worker handoff without entering real transcription/model
        # setup in the thread created by _release.
        controller._process_request = mock.Mock()
        start_recording(controller)

        controller._release()
        controller._worker.join(timeout=1)
        self.assertFalse(controller._worker.is_alive())
        controller._process_request.assert_called_once()

        self.assertIsNone(controller._watchdog_timer)
        self.assertEqual(controller.recorder.stopped, 1)
        time.sleep(0.35)
        self.assertEqual(controller.recorder.discarded, 0)

    def test_abort_clears_stale_busy_flag(self):
        controller = bare_controller()
        controller._busy = True
        start_recording(controller)

        controller._abort_recording("helper died")

        self.assertFalse(controller._busy)
        self.assertFalse(controller.has_live_work())

    def test_abort_of_confirmation_recording_keeps_request_answerable(self):
        controller = bare_controller()
        pending = controller._confirmation_broker.begin(
            "close_window", {}, "Close"
        )
        controller._busy = True
        controller.recorder.start()
        controller._confirmation_recording = True
        controller._confirmation_request_id = pending["request_id"]
        controller._acquire_recording()

        controller._helper_finished(0, None)

        self.assertFalse(controller._confirmation_recording)
        self.assertEqual(controller.recorder.discarded, 1)
        self.assertTrue(controller._confirmation_broker.is_pending())
        self.assertTrue(controller._busy, "request must stay in flight")
        self.assertIn(
            "dialog buttons", controller.response_changed.values[0]
        )


class FakeStream:
    def __init__(self):
        self.active = False
        self.stop_calls = 0

    def start(self):
        self.active = True

    def stop(self):
        self.active = False
        self.stop_calls += 1

    def close(self):
        self.active = False


def make_recorder(cap=MAX_QUEUED_BLOCKS):
    recorder = Recorder(max_queued_blocks=cap)
    recorder.stream = FakeStream()
    return recorder


def push_block(recorder, value=0.0):
    recorder._callback(np.full((16, 1), value, dtype="float32"), 16, None, None)


class RecorderCapTests(unittest.TestCase):
    """Recorder queue cap, dropped counter, queued_seconds(), discard()."""

    def test_default_cap_covers_two_minutes(self):
        self.assertGreaterEqual(MAX_QUEUED_BLOCKS * 512 / 16000, 120)

    def test_queue_drops_oldest_beyond_cap(self):
        recorder = make_recorder(cap=4)
        recorder.recording = True
        for value in range(10):
            push_block(recorder, value)

        self.assertEqual(recorder.audio_queue.qsize(), 4)
        self.assertEqual(recorder.dropped_blocks, 6)
        self.assertEqual(float(recorder.audio_queue.get_nowait()[0][0]), 6.0)

    def test_callback_ignores_audio_when_not_recording(self):
        recorder = make_recorder(cap=4)
        push_block(recorder)
        self.assertEqual(recorder.audio_queue.qsize(), 0)

    def test_queued_seconds(self):
        recorder = make_recorder(cap=8)
        recorder.recording = True
        for _ in range(8):
            push_block(recorder)
        self.assertAlmostEqual(recorder.queued_seconds(), 8 * 512 / 16000)

    def test_invalid_cap_rejected(self):
        with self.assertRaises(ValueError):
            Recorder(max_queued_blocks=0)

    def test_discard_stops_stream_and_clears_queue(self):
        recorder = make_recorder(cap=8)
        recorder.start()
        push_block(recorder, 1.0)

        self.assertTrue(recorder.discard())

        self.assertFalse(recorder.recording)
        self.assertFalse(recorder.stream.active)
        self.assertTrue(recorder.audio_queue.empty())
        self.assertIsNone(recorder.stop())

    def test_open_keeps_historical_attributes(self):
        recorder = make_recorder(cap=4)
        self.assertIs(recorder.audio_queue, recorder.queue)


class ShutdownOwnershipTests(unittest.TestCase):
    """B. Shutdown is fast and never nulls models a live worker still uses."""

    def test_blocked_preload_shutdown_returns_fast_and_keeps_models(self):
        order = []
        release = threading.Event()
        agent = BlockingAgent(order, gate=release)
        controller = bare_controller(order=order, agent=agent)

        preload = threading.Thread(
            target=controller._preload_agent, args=(agent,), daemon=True
        )
        preload.start()
        controller._agent_thread = preload

        # A live request worker owns the agent while shutdown runs.
        worker_stop = threading.Event()
        worker = threading.Thread(target=worker_stop.wait, daemon=True)
        worker.start()
        controller._worker = worker

        started = time.monotonic()
        controller.shutdown()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 3.0)
        self.assertTrue(agent.cancel_event.is_set())
        self.assertIs(controller._agent, agent)
        self.assertEqual(agent.stop_calls, 0)

        # Later: the request finishes and releases its own model exactly once.
        worker_stop.set()
        worker.join(2)
        release.set()
        preload.join(2)
        controller._cleanup_models(owner=agent)

        self.assertIsNone(controller._agent)
        self.assertEqual(agent.stop_calls, 1)

    def test_idle_shutdown_cleans_once_and_repeat_is_noop(self):
        order = []
        agent = BlockingAgent(order)
        agent.gate.set()
        agent.start()  # a loaded model that shutdown must release
        transcriber = OrderTranscriber(order)
        controller = bare_controller(
            order=order, agent=agent, transcriber=transcriber
        )
        controller._whisper_thread = None

        controller.shutdown()
        controller.shutdown()

        self.assertEqual(order.count("qwen-stop"), 1)
        self.assertEqual(order.count("whisper-unload"), 1)
        self.assertIsNone(controller._agent)
        self.assertIsNone(controller._transcriber)

    def test_shutdown_denies_pending_confirmation_by_id(self):
        controller = bare_controller()
        pending = controller._confirmation_broker.begin(
            "close_window", {}, "Close"
        )

        controller.shutdown()

        self.assertFalse(controller._confirmation_broker.is_pending())
        self.assertFalse(pending["approved"])
        self.assertEqual(controller.confirmation_resolved.values, [False])

    def test_shutdown_aborts_recording_and_stops_helper(self):
        controller = bare_controller(max_recording_seconds=5)
        start_recording(controller)
        controller.process = FakeHelperProcess()
        controller.process.state = lambda: 1  # Running

        controller.shutdown()

        self.assertFalse(controller._recording)
        self.assertEqual(controller.recorder.discarded, 1)
        self.assertEqual(controller.recorder.closed, 1)
        self.assertIsNone(controller._watchdog_timer)
        self.assertEqual(controller.process.terminated, 1)

    def test_worker_cleanup_after_shutdown_does_not_join_forever(self):
        order = []
        agent = BlockingAgent(order)
        agent.gate.set()
        controller = bare_controller(order=order, agent=agent)
        never = threading.Thread(target=threading.Event().wait, daemon=True)
        never.start()
        controller._whisper_thread = never
        controller._agent_thread = never

        started = time.monotonic()
        controller._cleanup_models(owner=agent, join_timeout=0.2)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.0)
        self.assertIsNone(controller._agent)


class MainShutdownTests(unittest.TestCase):
    """main.shutdown skips shared-service teardown while work is live."""

    def make_app(self, live):
        from app.main import MistyApplication

        application = MistyApplication.__new__(MistyApplication)
        closed = []

        class Controller:
            def shutdown(self):
                closed.append("controller")

            def has_live_work(self):
                return live

        class Service:
            def __init__(self, name):
                self.name = name

            def close(self):
                closed.append(self.name)

        application._shutting_down = False
        application.voice_controller = Controller()
        application.browser_session = Service("browser")
        application.reminder_store = Service("reminders")
        application.audit_log = Service("audit")
        application.clipboard_bridge = Service("clipboard")
        application.window_service = Service("windows")
        application.file_transcriber = Service("file-transcriber")
        application.popup_process = type("P", (), {
            "state": lambda self: 2,
            "terminate": lambda self: None,
            "kill": lambda self: None,
            "waitForFinished": lambda self, ms: True,
        })()
        application.popup_server = type("Ipc", (), {
            "send": lambda self, m: closed.append("ipc-send"),
            "close": lambda self: closed.append("ipc-close"),
        })()
        application.main_window = type(
            "W", (), {"hide": lambda self: closed.append("hide")}
        )()
        application.tray = type(
            "T", (), {"shutdown": lambda self: closed.append("tray")}
        )()
        application.reminder_timer = type("Q", (), {
            "stop": lambda self: closed.append("timer-stop"),
        })()
        return application, closed

    def test_live_work_keeps_shared_services_open(self):
        application, closed = self.make_app(live=True)

        application.shutdown()

        for name in ("browser", "reminders", "audit", "clipboard", "windows"):
            self.assertNotIn(name, closed)
        for name in ("controller", "timer-stop", "ipc-close", "tray", "hide",
                     "file-transcriber"):
            self.assertIn(name, closed)

    def test_idle_shutdown_closes_services(self):
        application, closed = self.make_app(live=False)

        application.shutdown()

        for name in ("browser", "reminders", "audit", "clipboard", "windows"):
            self.assertIn(name, closed)


class ModelOrderTests(unittest.TestCase):
    """C. Preload joined before whisper; Qwen released before VLM/file whisper."""

    def test_preload_joined_before_transcribe(self):
        order = []
        release = threading.Event()
        agent = BlockingAgent(order, gate=release)
        controller = bare_controller(order=order, agent=agent)
        transcriber = OrderTranscriber(order)
        controller._transcriber = transcriber

        preload = threading.Thread(target=controller._preload_agent,
                                   args=(agent,), daemon=True)
        controller._agent_thread = preload
        preload.start()

        joined = {}

        def worker():
            joined["value"] = controller._join_preload_before_transcribe()
            transcriber.transcribe([0.1])

        requester = threading.Thread(target=worker, daemon=True)
        requester.start()
        time.sleep(0.1)
        release.set()
        requester.join(5)

        self.assertTrue(joined["value"])
        self.assertLess(
            order.index("qwen-load"), order.index("whisper-transcribe")
        )

    def test_join_is_bounded_when_preload_hangs(self):
        order = []
        agent = BlockingAgent(order)
        controller = bare_controller(order=order, agent=agent)
        preload = threading.Thread(target=agent.start, daemon=True)
        preload.start()
        controller._agent_thread = preload

        started = time.monotonic()
        joined = controller._join_preload_before_transcribe()
        elapsed = time.monotonic() - started

        self.assertFalse(joined)
        self.assertLess(elapsed, 3.5)
        agent.gate.set()
        preload.join(2)

    def test_unload_reasoning_model_only_for_the_running_request(self):
        order = []
        agent = BlockingAgent(order)
        agent.gate.set()
        agent.start()
        controller = bare_controller(order=order, agent=agent)
        controller._agent_busy = True

        # A Qt-thread call outside the request never stops a running agent.
        self.assertFalse(controller.unload_reasoning_model())
        self.assertEqual(agent.stop_calls, 0)

        # The request worker itself releases its own model once.
        controller._worker = threading.current_thread()
        self.assertTrue(controller.unload_reasoning_model())
        self.assertEqual(agent.stop_calls, 1)

    def test_unload_reasoning_model_ignored_when_idle(self):
        controller = bare_controller(agent=None)
        self.assertFalse(controller.unload_reasoning_model())

    def test_controller_wires_file_transcriber_hook(self):
        order = []
        file_transcriber = FakeFileTranscriber(order)
        controller = bare_controller(order=order,
                                     file_transcriber=file_transcriber)
        calls = []

        self.assertTrue(
            controller.set_before_transcribe(lambda: calls.append("qwen"))
        )
        file_transcriber.transcribe("folder", "a.wav")

        self.assertEqual(calls, ["qwen"])
        self.assertEqual(order, ["file-whisper"])

    def test_vision_runs_capture_ocr_vlm_without_releasing_planner(self):
        from app.desktop.vision.coordinator import DesktopVision

        calls = []

        class Capture:
            def capture(self, **kwargs):
                calls.append("capture")
                return type("C", (), {
                    "image": object(), "width": 1, "height": 1,
                    "captured_at": "now",
                })()

        class Ocr:
            def recognize(self, image):
                calls.append("ocr")
                return object()

        class Vlm:
            def analyze(self, image, question):
                calls.append("vlm")
                return object()

        DesktopVision(Capture(), Ocr(), Vlm()).inspect()

        self.assertEqual(calls, ["capture", "ocr", "vlm"])

    def test_file_transcriber_hook_failure_never_breaks_transcription(self):
        from app.speech import file_transcriber

        def boom():
            raise RuntimeError("model busy")

        engine = object.__new__(file_transcriber.FileTranscriber)
        engine.before_transcribe = boom
        engine._closed = False
        with mock.patch.object(recorder_module, "np", recorder_module.np):
            engine._run_before_transcribe()  # must not raise
        engine.close()
        self.assertTrue(engine._closed)




class CancelEventIntegrationTests(unittest.TestCase):
    """Real MistyAgent.run with a scripted model: Qwen dispose vs cancel."""

    class FakeProcess:
        """A server process that ignores SIGTERM until it is killed."""

        def __init__(self, owner=None):
            self.killed = 0
            self.exited = False
            self.owner = owner

        def poll(self):
            return 0 if self.exited else None

        def terminate(self):
            # SIGTERM is ignored: the object stays alive for stop().
            pass

        def kill(self):
            self.killed += 1
            self.exited = True

        def wait(self, timeout=None):
            if not self.exited:
                raise subprocess.TimeoutExpired("llama-server", timeout)
            return 0

    def make_agent(self, hook, controller):
        from tests.agent_fakes import FakeRegistry, TestableMistyAgent

        class HookedAgent(TestableMistyAgent):
            def __init__(self):
                super().__init__(FakeRegistry(), [])
                # agent.run() itself calls start(); the model server exists
                # from then on until stop() disposes it.
                self.process = self_outer.FakeProcess(owner=None)
                self.cancel_event = threading.Event()
                self.chat_calls_done = 0
                self.cancel_seen_by_model = []

            def stop(self):
                # Mirror MistyAgent.stop(): SIGTERM, then kill after the
                # grace period; the server ignores SIGTERM.
                if self.process is not None:
                    if self.process.poll() is None:
                        self.process.terminate()
                        try:
                            self.process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            self.process.kill()
                            self.process.wait(timeout=5)
                    self.process = None

            def _chat(self, messages, tools=None, response_format=None):
                self.chat_calls_done += 1
                self.cancel_seen_by_model.append(self.cancel_event.is_set())
                hook(controller, self)
                raise RuntimeError("scripted model stop")

        self_outer = self
        return HookedAgent()

    def run_request(self, controller, agent):
        from app.engine import voice_controller as module

        original = module.MistyAgent
        module.MistyAgent = lambda **_kwargs: agent
        try:
            # ``_run_agent_session`` creates the agent from module.MistyAgent
            # (patched below) and runs the body with routing state set up.
            worker = threading.Thread(
                target=controller._process_typed_request,
                args=("summarize the notes in my documents folder",),
                daemon=True,
            )
            controller._worker = worker
            controller._busy = True
            worker.start()
            worker.join(5)
            self.assertFalse(worker.is_alive())
        finally:
            module.MistyAgent = original

    def controller(self):
        controller = bare_controller()
        # The session wrapper builds a registry; the test never needs one.
        controller.memory_store = None
        controller.audit_log = None
        controller.task_journal = None
        controller._build_registry = lambda: None
        controller._request_origins = {}
        controller._request_texts = {}
        return controller

    def test_unload_disposes_model_once_and_keeps_cancellation(self):
        controller = self.controller()
        state = {}

        def shutdown_then_unload(ctrl, agent):
            # What the session wrapper publishes before body() runs.
            ctrl._agent = agent
            ctrl._agent_busy = True
            state["cancel_before"] = agent.cancel_event.is_set()
            ctrl._shutting_down = True
            agent.cancel_event.set()
            state["released"] = ctrl.unload_reasoning_model()
            state["cancel_after"] = agent.cancel_event.is_set()

        agent = self.make_agent(shutdown_then_unload, controller)
        process = agent.process
        controller._agent = agent
        controller._agent_busy = True

        self.run_request(controller, agent)

        self.assertFalse(state["cancel_before"])
        self.assertTrue(state["released"])
        self.assertTrue(state["cancel_after"],
                        "unload must not clear the shutdown cancellation")
        self.assertEqual(process.killed, 1)
        self.assertEqual(agent.chat_calls_done, 1)
        self.assertIsNone(controller._agent)

    def test_unload_releases_an_uncancelled_request(self):
        controller = self.controller()
        calls = []

        def unload_only(ctrl, agent):
            ctrl._agent = agent
            ctrl._agent_busy = True
            calls.append(ctrl.unload_reasoning_model())

        agent = self.make_agent(unload_only, controller)
        process = agent.process
        controller._agent = agent
        controller._agent_busy = True

        self.run_request(controller, agent)

        self.assertEqual(calls, [True])
        self.assertFalse(agent.cancel_event.is_set())
        self.assertEqual(process.killed, 1)

    def test_unload_outside_the_request_worker_never_stops_an_agent(self):
        controller = self.controller()
        agent = self.make_agent(lambda *_: None, controller)
        controller._agent = agent
        controller._agent_busy = True

        self.assertFalse(controller.unload_reasoning_model())
        self.assertEqual(agent.process.killed, 0)


if __name__ == "__main__":
    unittest.main()
