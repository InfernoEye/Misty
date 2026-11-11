"""Typed request state: admission, origin, failures, selection, clarification.

The typed path must never rewrite the origin of an in-flight voice request,
must make its failures visible in chat, must honour pending selections, and
must carry a pending clarification into the next request.
"""

import os
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject  # noqa: E402

from app.engine import voice_controller as vc  # noqa: E402
from app.engine.confirmations import ConfirmationBroker  # noqa: E402
from app.engine.voice_controller import (  # noqa: E402
    TypedAdmission,
    VoiceController,
)


# ---------------------------------------------------------------- doubles


class Collector:
    """Signal double: records emits, and fans out to connected callables."""

    def __init__(self):
        self.values = []
        self.receivers = []

    def emit(self, *values):
        self.values.append(values[0] if len(values) == 1 else values)
        for receiver in list(self.receivers):
            receiver(*values)

    def connect(self, receiver, *_args, **_kwargs):
        self.receivers.append(receiver)
        return None


class FakePopup:
    def __init__(self):
        self.message_received = Collector()
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        return True


class FakeProcess:
    def state(self):
        return None

    def setProcessEnvironment(self, *_args):
        pass

    def start(self, *_args):
        pass


class FakeAgent:
    """Records run() calls and returns a canned answer."""

    def __init__(self, answer="Done.", error=None):
        self.answer = answer
        self.error = error
        self.calls = []
        self.process = None

    # Mirrors MistyAgent.run(transcript, context=None) (the real contract).
    def run(self, transcript, context=None):
        self.calls.append((transcript, {"context": context}))
        if self.error is not None:
            raise self.error
        return self.answer

    def stop(self):
        pass


class FakeAgentWithoutContext(FakeAgent):
    def run(self, transcript):
        self.calls.append((transcript, {}))
        return self.answer


def _fill_missing(controller):
    """Give the __new__ instance every constructor attribute (None/scalar)."""
    for name, default in _INIT_DEFAULTS.items():
        if not hasattr(controller, name):
            setattr(controller, name, default() if callable(default) else default)


_INIT_DEFAULTS = {
    "audit_log": None,
    "task_journal": None,
    "knowledge_index": None,
    "web_search_client": None,
    "browser_session": None,
    "reminder_store": None,
    "file_transfer_engine": None,
    "zip_engine": None,
    "text_search_service": None,
    "directory_transfer_engine": None,
    "calendar_engine": None,
    "duplicate_finder": None,
    "file_transcriber": None,
    "control_activator": None,
    "media_controller": None,
    "active_window_provider": None,
    "vision_service": None,
    "clipboard_bridge": None,
    "popup_hovered": False,
    "_completion_waiting_for_hover": False,
    "_agent_errors": list,
    "memory_store": None,
    "_popup_window_selection": None,
}



def hold_live_work(controller):
    """Make ``has_live_work`` true the way a real in-flight request does.

    Admission is gated on live work, not on the ``_busy`` flag (a stale flag
    from an aborted recording must not block typing), so tests simulate a
    request by starting a real worker thread that waits for release.
    """
    release = threading.Event()

    def wait():
        release.wait(5)

    worker = threading.Thread(target=wait, daemon=True)
    controller._worker = worker
    worker.start()
    return release



def module_level_no_session_turn():
    """The controller's private "no turn" sentinel."""
    from app.engine import voice_controller as vc

    return vc._NO_SESSION_TURN
def build_controller():
    """A controlled VoiceController instance (no window/registry wiring)."""
    # ``__new__`` alone skips QObject.__init__, leaving the C++ side
    # unconstructed: every signal then raises "Signal source has been
    # deleted". Run the base initialiser against the half-built object.
    controller = VoiceController.__new__(VoiceController)
    QObject.__init__(controller)
    controller.popup_ipc = FakePopup()
    controller.window_service = None
    controller._shutting_down = False
    controller._recording = False
    controller._confirmation_recording = False
    controller._busy = False
    controller.shortcut = "F8"
    controller._worker = None
    controller._agent = None
    controller._transcriber = None
    controller._whisper_thread = None
    controller._agent_thread = None
    controller._window_selection_lock = threading.Lock()
    controller._pending_window_selection = None
    controller._pending_window_selection_timer = None
    controller._completion_timer = type(
        "Timer", (), {"stop": lambda *_a, **_k: None}
    )()
    controller._confirmation_broker = ConfirmationBroker(timeout=60)
    controller._active_typed_request_id = None
    controller._typed_request_text = ""
    controller._typed_outcomes = []
    controller._pending_clarification = None
    controller._clarification_flag = threading.Event()
    controller._latest_request_id = None
    controller._request_origins = {}
    controller._request_texts = {}
    # Lifecycle state added when recording abort / shutdown ownership landed.
    controller._start_error_thread = None
    # Session-context bookkeeping added with the QOL request-context work.
    controller._session_turn = module_level_no_session_turn()
    controller._session_events = []
    controller.session_context = None
    controller._cleanup_lock = threading.RLock()
    controller._agent_busy = False
    controller.max_recording_seconds = 120
    controller._recording_lock = threading.RLock()
    controller._confirmation_request_id = None
    controller._confirmation_worker = None
    controller._preload_thread = None
    controller._watchdog_timer = None
    controller.recorder = type(
        "Recorder",
        (),
        {"discard": lambda _self: None, "stop": lambda _self: None},
    )()
    controller._finish_recording = lambda: None
    for name in (
        "state_changed", "transcript_changed", "response_changed",
        "activity_event", "confirmation_requested", "confirmation_resolved",
        "window_selection_requested", "window_selection_message_requested",
        "window_selection_cleared", "ready_reset_requested",
        "typed_rejected", "typed_failed", "typed_completed",
    ):
        setattr(controller, name, Collector())
    controller._typed_cleanup = lambda owner=None, **_kwargs: None
    controller._cleanup_models = controller._typed_cleanup
    _fill_missing(controller)
    return controller


def wait_idle(controller, timeout=5):
    worker = controller._worker
    if worker is not None:
        worker.join(timeout)
        assert not worker.is_alive(), "typed worker did not finish"


class FakeWindow:
    """Chat-surface double for the MainWindow methods main.py calls."""

    def __init__(self):
        self.messages = []
        self.errors = []
        self.input_text = ""
        self.status = ""
        self.focused = 0

    def append_user_message(self, text):
        self.messages.append(("user", text))

    def append_assistant_message(self, text):
        self.messages.append(("misty", text))

    def append_assistant_error(self, text):
        self.errors.append(text)
        self.messages.append(("error", text))

    def restore_chat_input(self, text):
        self.input_text = text

    def focus_chat_reply_target(self):
        self.focused += 1

    def refresh_shortcut_hint(self):
        pass

    def show_shortcut_failure(self, reason):
        self.status = "failure"


class FakeResponseWindow:
    def __init__(self):
        self.shown = []

    def show_response(self, text, transcript=None):
        self.shown.append(text)


def load_typed_main_methods():
    """Reuse the real main.py methods without constructing a QApplication."""
    from app import main as main_module

    class Harness:
        _submit_typed_text = main_module.MistyApplication._submit_typed_text
        _maybe_show_full_response = (
            main_module.MistyApplication._maybe_show_full_response
        )
        _current_request_origin = (
            main_module.MistyApplication._current_request_origin
        )
        _handle_typed_failed = (
            main_module.MistyApplication._handle_typed_failed
        )
        _handle_typed_completed = (
            main_module.MistyApplication._handle_typed_completed
        )
        _handle_voice_state = (
            main_module.MistyApplication._handle_voice_state
        )

        def __init__(self, controller, window):
            self.voice_controller = controller
            self.main_window = window
            self.response_window = FakeResponseWindow()
            self._latest_response = ""
            self._latest_transcript = ""
            self._last_shown_pair = None
            self._last_chat_pair = None
            self._typed_request = False
            self._active_request_id = None
            self._request_tools = set()
            self._shutting_down = False

    return Harness


# ---------------------------------------------------------------- tests


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.controller = build_controller()

    def test_busy_rejection_is_not_accepted_and_has_reason(self):
        self._release = hold_live_work(self.controller)
        self.addCleanup(self._release.set)
        result = self.controller.submit_text("hello")
        self.assertIsInstance(result, TypedAdmission)
        self.assertFalse(result.accepted)
        self.assertIn("busy", result.reason)
        self.assertIsNone(self.controller._active_typed_request_id)
        accepted, reason, request_id = result
        self.assertFalse(accepted)

    def test_oversize_rejection_has_exact_message(self):
        result = self.controller.submit_text("x" * 2001)
        self.assertFalse(result.accepted)
        self.assertEqual(
            result.reason,
            "That request is too long. Please keep it under 2000 characters.",
        )

    def test_acceptance_records_origin_and_request_id(self):
        self.controller._process_typed_request = lambda *_a: None
        self.controller._build_registry = lambda: None
        result = self.controller.submit_text("open the browser")
        wait_idle(self.controller)
        self.assertTrue(result.accepted)
        self.assertTrue(result.request_id)
        self.assertEqual(
            self.controller.current_request_origin(), "typed"
        )
        self.assertEqual(
            self.controller._request_origins[result.request_id], "typed"
        )


class OriginTests(unittest.TestCase):
    def setUp(self):
        self.controller = build_controller()
        self.Harness = load_typed_main_methods()
        self.window = FakeWindow()
        self.app = self.Harness(self.controller, self.window)

    def test_rejected_typed_does_not_flip_voice_origin(self):
        # Voice request A is in flight.
        self._release = hold_live_work(self.controller)
        self.addCleanup(self._release.set)
        self.controller._mark_request_origin("voice-A", "voice", "long one")
        # A typed request arrives and is rejected because A is busy.
        self._release = hold_live_work(self.controller)
        self.addCleanup(self._release.set)
        self.app._submit_typed_text("typed one")
        self.assertFalse(self.app._typed_request)
        self.assertIsNone(self.app._active_request_id)
        self.assertEqual(
            self.controller.current_request_origin(), "voice"
        )
        self.assertIn("typed one", self.window.input_text)

    def test_long_voice_response_still_shown_after_rejected_typed(self):
        self._release = hold_live_work(self.controller)
        self.addCleanup(self._release.set)
        self.controller._mark_request_origin("voice-A", "voice", "long one")
        self.app._submit_typed_text("typed one")
        long_response = "Line of results.\n" * 40
        self.app._latest_response = long_response
        self.app._latest_transcript = "long one"
        self.app._maybe_show_full_response()
        self.assertTrue(self.app.response_window.shown)
        self.assertIn("Line of results.", self.app.response_window.shown[0])
        pair = ("long one", long_response.strip())
        self.assertEqual(self.app._last_shown_pair, pair)

    def test_typed_response_does_not_open_full_window(self):
        self.controller._process_typed_request = lambda *_a: None
        self.controller._build_registry = lambda: None
        self.app._submit_typed_text("typed one")
        wait_idle(self.controller)
        self.app._latest_response = "Line of results.\n" * 40
        self.app._maybe_show_full_response()
        self.assertEqual(self.app.response_window.shown, [])


class VisibleFailureTests(unittest.TestCase):
    def setUp(self):
        self.controller = build_controller()
        self.Harness = load_typed_main_methods()
        self.window = FakeWindow()
        self.app = self.Harness(self.controller, self.window)
        self.controller.typed_failed.connect(self.app._handle_typed_failed)
        self.controller.typed_completed.connect(
            self.app._handle_typed_completed
        )
        # Wire the real state handler too: it must not pre-empt the
        # request-id-keyed failure callback.
        self.controller.state_changed.connect(self.app._handle_voice_state)

    def test_busy_rejection_restores_input_and_bubble_without_user_echo(self):
        self._release = hold_live_work(self.controller)
        self.addCleanup(self._release.set)
        self.app._submit_typed_text("please do this")
        self.assertEqual(self.window.input_text, "please do this")
        self.assertEqual(len(self.window.errors), 1)
        self.assertIn("busy", self.window.errors[0])
        self.assertNotIn(("user", "please do this"), self.window.messages)

    def test_accepted_request_echoes_user_once(self):
        self.controller._process_typed_request = lambda *_a: None
        self.controller._build_registry = lambda: None
        self.app._submit_typed_text("admitted")
        wait_idle(self.controller)
        self.assertEqual(self.window.messages.count(("user", "admitted")), 1)

    def test_typed_exception_shows_assistant_error_bubble(self):
        # The controller's signal is re-emitted on the worker thread. In a
        # headless run there is no event loop to deliver that queued signal to
        # the harness handler, so this test drives the real failure handler
        # directly from the recorded emission instead of depending on thread
        # timing. The signal payload is asserted first.
        agent = FakeAgent(error=RuntimeError("model crashed"))
        self.controller._agent = agent
        self.controller._build_registry = lambda: None
        self.controller._typed_cleanup = lambda owner=None, **_kwargs: None

        # Do not replace the signal object: setUp already connected the real
        # handler to the controller's signal, and a fresh Collector() would
        # orphan that connection. Observe the real signal instead.
        seen = []
        self.controller.typed_failed.connect(
            lambda *args: seen.append(args)
        )

        from app.engine import voice_controller as module

        original = module.MistyAgent
        module.MistyAgent = lambda **_kwargs: agent
        try:
            # Admit through the real entry point so main tracks the request.
            self.app._submit_typed_text("do the thing")
            wait_idle(self.controller)
        finally:
            module.MistyAgent = original

        self.assertTrue(seen, "typed_failed was never emitted")
        request_id, _text, message = seen[0]
        self.assertEqual(
            message, "Something went wrong: model crashed"
        )
        # main only shows the bubble for the request it admitted.
        self.app._active_request_id = request_id
        self.app._handle_typed_failed(request_id, "do the thing", message)
        self.assertTrue(self.window.errors, [
            self.app.__class__.__name__,
            self.window is self.app.main_window,
            len(self.window.messages),
            seen,
            self.app._active_request_id,
        ])
        self.assertTrue(seen)
        self.assertEqual(seen[0][2], "Something went wrong: model crashed")
        self.assertIsNone(self.app._active_request_id)
        self.assertIn("model crashed", self.window.errors[-1])
        self.assertEqual(
            self.controller.state_changed.values[-1], "error"
        )
        self.assertIsNone(self.app._active_request_id)
        self.assertFalse(self.app._typed_request)


class TypedSelectionTests(unittest.TestCase):
    def setUp(self):
        self.controller = build_controller()
        self.agent = FakeAgent()
        self.controller._build_registry = lambda: None
        self.controller._agent = self.agent

        from app.engine import voice_controller as module

        self._original_agent = module.MistyAgent
        module.MistyAgent = lambda **_kwargs: self.agent
        self.addCleanup(
            setattr, module, "MistyAgent", self._original_agent
        )

    def _pending(self):
        self.controller._set_pending_window_selection(
            "focus_window",
            [
                {"id": "1", "title": "Editor"},
                {"id": "2", "title": "Terminal"},
            ],
        )

    def _capture_selection_resolution(self):
        """Record the exact argument the typed path hands the resolver."""
        calls = []
        original = vc.VoiceController._handle_pending_window_selection

        def recording(self_, transcript):
            calls.append(transcript)
            return original(self_, transcript)

        vc.VoiceController._handle_pending_window_selection = recording
        self.addCleanup(
            setattr, vc.VoiceController,
            "_handle_pending_window_selection", original,
        )
        return calls

    def test_typed_two_resolves_pending_window_selection(self):
        self._pending()
        calls = self._capture_selection_resolution()
        # make the chosen window "no longer open" -> a definite resolver
        # answer without executing anything or asking for confirmation.
        self.controller.window_service = type(
            "Windows",
            (),
            {"list_windows": lambda _self: {"success": True, "windows": []}},
        )()
        result = self.controller.submit_text("2")
        wait_idle(self.controller)

        self.assertEqual(calls, ["2"])  # same resolver the voice path uses
        self.assertFalse(self.controller._has_pending_window_selection())
        self.assertIsNone(self.controller._pending_window_selection_timer)
        self.assertEqual(self.agent.calls, [])  # agent never ran
        self.assertEqual(self.controller.state_changed.values[-1], "complete")
        request_id, text, kind = self.controller.typed_completed.values[-1]
        self.assertEqual(request_id, result.request_id)
        self.assertEqual(text, "2")
        self.assertEqual(kind, "selection")

    def test_typed_cancel_clears_selection(self):
        self._pending()
        calls = self._capture_selection_resolution()
        self.controller.submit_text("cancel")
        wait_idle(self.controller)
        self.assertEqual(calls, ["cancel"])
        self.assertFalse(self.controller._has_pending_window_selection())
        self.assertIsNone(self.controller._pending_window_selection_timer)
        self.assertEqual(self.agent.calls, [])
        self.assertEqual(
            self.controller.typed_completed.values[-1][2], "selection"
        )

    def test_typed_request_without_selection_runs_agent(self):
        calls = self._capture_selection_resolution()
        self.controller.submit_text("open the terminal")
        wait_idle(self.controller)
        # Same flow as voice: the resolver is consulted and returns None.
        self.assertEqual(calls, ["open the terminal"])
        self.assertEqual(len(self.agent.calls), 1)


class ClarificationTests(unittest.TestCase):
    def setUp(self):
        self.controller = build_controller()
        self.controller._build_registry = lambda: None

        from app.engine import voice_controller as module

        self._module = module
        self.agent = FakeAgent(answer="Which folder? Downloads or Desktop?")
        self._original_agent = module.MistyAgent
        module.MistyAgent = lambda **_kwargs: self.agent
        self.addCleanup(
            setattr, module, "MistyAgent", self._original_agent
        )

    def run_typed(self, text, agent=None):
        agent = agent or self.controller._agent
        module = self._module
        original = module.MistyAgent
        module.MistyAgent = lambda **_kwargs: agent
        try:
            self.controller._agent = agent
            self.controller._build_registry = lambda: None
            self.controller.submit_text(text)
            wait_idle(self.controller)
        finally:
            module.MistyAgent = original
        return agent

    def test_clarification_persisted_after_run_and_forwarded(self):
        text = "organise my files"
        question = "Which folder: Downloads or Desktop?"

        class ClarifyingAgent(FakeAgent):
            def __init__(self, controller):
                super().__init__()
                self.controller = controller
                self.mode = "clarify"

            def run(self, transcript, context=None):
                self.calls.append((transcript, {"context": context}))
                if self.mode == "clarify":
                    # The agent signals a clarification via its event stream.
                    self.controller._agent_activity_event({
                        "kind": "decision",
                        "title": "Clarification needed",
                        "details": question,
                        "status": "planned",
                    })
                    return question
                return "Moved Downloads."

        agent = ClarifyingAgent(self.controller)
        self.run_typed(text, agent=agent)
        pending = self.controller._pending_clarification
        self.assertIsNotNone(pending)
        self.assertEqual(pending["transcript"], text)
        self.assertEqual(pending["question"], question)
        self.assertIsInstance(pending["created"], float)

        # The user answers; the next run must receive the clarification.
        agent.mode = "answer"
        # The agent double is a plain object; keep the real event
        # callback so the controller's task/session bookkeeping runs.
        agent.cancel_event = threading.Event()
        self.run_typed("Downloads", agent=agent)
        transcript, kwargs = agent.calls[-1]
        self.assertEqual(transcript, "Downloads")
        # Contract: the controller passes a mapping with the flat
        # clarification (and a bounded "session" block when one exists).
        context = kwargs.get("context") or {}
        self.assertEqual(
            {"transcript": context.get("transcript"),
             "question": context.get("question")},
            {"transcript": text, "question": question},
        )
        self.assertIn("session", context)
        self.assertIsNone(self.controller._pending_clarification)

    def test_activity_flag_alone_does_not_persist(self):
        # A "Clarification needed" event from an unrelated activity push is
        # ignored unless the agent answered with a question.
        self.controller._session_begin_turn("hello")
        self.controller._agent_activity_event({
            "kind": "decision",
            "title": "Clarification needed",
            "details": "stale question",
            "status": "planned",
        })
        # The agent answered with something that is not a clarification.
        self.controller._typed_run_agent(
            type("A", (), {"run": lambda _self, text, context=None: "Done."})(),
            "hello",
        )
        self.assertIsNone(self.controller._pending_clarification)

    def test_agent_without_context_gets_fallback_prompt(self):
        agent = FakeAgentWithoutContext()
        self.controller._pending_clarification = {
            "transcript": "open the report",
            "question": "Which report?",
            "created": time.monotonic(),
        }
        self.run_typed("quarterly one", agent=agent)
        transcript, _kwargs = agent.calls[0]
        self.assertIn("Original request: open the report", transcript)
        self.assertIn("Clarification asked: Which report?", transcript)
        self.assertIn("User answer: quarterly one", transcript)

    def test_expired_clarification_is_not_forwarded(self):
        agent = FakeAgent()
        self.controller._pending_clarification = {
            "transcript": "open the report",
            "question": "Which report?",
            "created": time.monotonic() - 301,
        }
        self.run_typed("never mind", agent=agent)
        self.assertEqual(agent.calls[0], ("never mind", {"context": None}))
        self.assertIsNone(self.controller._pending_clarification)

    def test_selection_answer_does_not_consume_clarification_prompt(self):
        agent = FakeAgent()
        self.controller._pending_clarification = {
            "transcript": "open the report",
            "question": "Which report?",
            "created": time.monotonic(),
        }
        # numeric/selection words pass through, but the pending context is
        # dropped rather than misapplied.
        self.controller.submit_text("2")
        wait_idle(self.controller)
        self.assertIsNone(self.controller._pending_clarification)


if __name__ == "__main__":
    unittest.main()
