"""Shortcut registration must be proven, not assumed.

A saved chord is not an active chord. These tests drive the real controller
methods with a fake QProcess so the READY handshake, rollback and retry
behaviour is checked without touching the live KDE shortcut system or the
real config file.
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QProcess  # noqa: E402

from app.engine import voice_controller as controller_module  # noqa: E402
from app.engine.voice_controller import VoiceController  # noqa: E402


def _qt_app():
    """Return the process-wide application (shared with every Qt test).

    Creating a second application object, or letting the only one be
    garbage-collected while a window still exists, crashes Qt. A bare
    QCoreApplication also makes any later widget test abort, so the shared
    helper always yields a widget-capable QApplication.
    """
    global _APP
    from tests.agent_fakes import qt_application

    _APP = qt_application()
    return _APP


class FakeProcess:
    """Minimal stand-in for QProcess as used by the shortcut handshake."""

    def __init__(self):
        self.state_value = QProcess.ProcessState.NotRunning
        self._error = ""
        self.sequences = []

    def state(self):
        return self.state_value

    def errorString(self):
        return self._error

    def waitForStarted(self, _timeout):
        self.state_value = QProcess.ProcessState.Running
        return True

    def terminate(self):
        self.state_value = QProcess.ProcessState.NotRunning

    def kill(self):
        self.state_value = QProcess.ProcessState.NotRunning

    def waitForFinished(self, _timeout):
        self.state_value = QProcess.ProcessState.NotRunning
        return True

    def setProcessEnvironment(self, environment):
        self._environment = environment


class FakePopup:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        return True


class _QuietSignals:
    """Collect emissions without a Qt receiver."""

    def __init__(self):
        self.values = []

    def emit(self, *values):
        self.values.append(values)


class _FakeRecorder:
    def __init__(self):
        self.discarded = 0

    def discard(self):
        self.discarded += 1

    def stop(self):
        return None


def make_controller(ready_sequences=("Launch (5)",), error_reason=""):
    controller = VoiceController.__new__(VoiceController)
    # State owned by the real __init__ that set_shortcut's failure path
    # touches. Building the object by hand keeps the test off the audio and
    # desktop services while still exercising the shipped code.
    controller._recording_lock = controller_module.threading.RLock()
    controller._recording = False
    controller._confirmation_recording = False
    controller._confirmation_request_id = None
    controller._busy = False
    controller._worker = None
    controller._watchdog_timer = None
    controller.recorder = _FakeRecorder()
    controller.activity_event = _QuietSignals()
    controller.state_changed = _QuietSignals()
    controller.response_changed = _QuietSignals()
    controller.ready_reset_requested = _QuietSignals()
    controller._pending_window_selection = None
    controller._pending_window_selection_timer = None
    controller._finish_recording = lambda: None
    controller._has_pending_window_selection = lambda: False
    controller.process = FakeProcess()
    controller.popup_ipc = FakePopup()
    controller.shortcut = "Launch (5)"
    controller._shortcut_saved = "Launch (5)"
    controller._shortcut_active = None
    controller._shortcut_apply_lock = controller_module.threading.RLock()
    controller._helper_ready = False
    controller._helper_ready_event = controller_module.threading.Event()
    controller._helper_error_reason = ""
    controller._restarting_helper = False
    controller._shutting_down = False
    controller._shortcut_suspended = False
    controller._buffer = ""
    controller._reported = []

    def launch(sequence):
        controller._helper_ready = False
        controller._helper_ready_event.clear()
        controller._helper_error_reason = ""
        controller.process.sequences.append(sequence)
        controller.process.state_value = QProcess.ProcessState.Running
        if error_reason and sequence not in ready_sequences:
            controller._helper_error_reason = error_reason
            return
        if sequence in ready_sequences:
            controller._helper_ready = True
            controller._shortcut_active = sequence
            controller._helper_ready_event.set()

    controller._shortcut_launch_helper = launch
    # The fake helper reports readiness synchronously; the event-loop pump
    # only exists to observe a real child process, so skip it.
    controller._shortcut_pump = staticmethod(lambda _milliseconds: None)

    def report(success, reason=""):
        controller._reported.append((bool(success), reason))

    controller._shortcut_report = report
    return controller


class ShortcutReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _qt_app()

    def test_success_requires_the_ready_line(self):
        controller = make_controller(ready_sequences=("Ctrl+Alt+M",))

        applied = controller.set_shortcut("Ctrl+Alt+M")

        self.assertTrue(applied)
        self.assertEqual(controller.shortcut, "Ctrl+Alt+M")
        self.assertEqual(controller._reported[-1][0], True)

    def test_launch_without_ready_is_not_success(self):
        # The helper starts but never confirms: the old code reported success
        # after waitForStarted alone.
        controller = make_controller(ready_sequences=())

        with mock.patch.object(
            controller_module, "SHORTCUT_READY_TIMEOUT_MS", 1
        ):
            applied = controller.set_shortcut("Ctrl+Alt+M")

        self.assertFalse(applied)
        self.assertEqual(controller.shortcut, "Launch (5)")
        ok, reason = controller._reported[-1]
        self.assertFalse(ok)
        self.assertIn("READY", reason)

    def test_registration_error_is_reported_and_previous_chord_restored(self):
        controller = make_controller(
            ready_sequences=("Launch (5)",),
            error_reason="registration rejected",
        )

        with mock.patch.object(
            controller_module, "SHORTCUT_READY_TIMEOUT_MS", 1
        ):
            applied = controller.set_shortcut("Ctrl+Alt+M")

        self.assertFalse(applied)
        # Rollback relaunched the previous chord.
        self.assertEqual(
            controller.process.sequences[-1], "Launch (5)"
        )
        ok, reason = controller._reported[-1]
        self.assertFalse(ok)
        self.assertIn("registration rejected", reason)

    def test_already_bound_chord_is_accepted_without_a_restart(self):
        controller = make_controller()
        controller._helper_ready = True
        controller._shortcut_active = "Launch (5)"

        applied = controller.set_shortcut("Launch (5)")

        self.assertTrue(applied)
        self.assertEqual(controller.process.sequences, [])

    def test_suspended_binding_can_be_resumed(self):
        controller = make_controller(ready_sequences=("Launch (5)",))
        controller._helper_ready = True
        controller._shortcut_active = "Launch (5)"
        controller.suspend_shortcut()
        self.assertTrue(controller._shortcut_suspended)
        self.assertFalse(controller._helper_ready)

        resumed = controller.resume_shortcut()

        self.assertTrue(resumed)
        self.assertTrue(controller._helper_ready)
        self.assertEqual(controller._shortcut_active, "Launch (5)")

    def test_multi_chord_and_control_characters_are_rejected(self):
        controller = make_controller()
        for value in ("Ctrl+K, Ctrl+M", "bad\nkey", "", "x" * 65):
            with self.subTest(value=value):
                self.assertFalse(controller.set_shortcut(value))


if __name__ == "__main__":
    unittest.main()
