"""Escape cancels a running task only while one is running.

These tests use the real ``misty-shortcut`` helper binary. Escape cannot be
pressed here, so they check the parts a test can observe: the helper accepts
the bind/unbind commands and stays alive, and a pending approval is denied
when a cancel is requested.
"""

import os
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QProcess  # noqa: E402

from app.engine import voice_controller as controller_module  # noqa: E402
from app.engine.confirmations import ConfirmationBroker  # noqa: E402
from app.engine.voice_controller import VoiceController  # noqa: E402
from tests.agent_fakes import qt_application  # noqa: E402

HELPER = Path(controller_module.HELPER)


@unittest.skipUnless(
    HELPER.is_file() and os.environ.get("MISTY_LIVE_SHORTCUT_TESTS") == "1",
    "live KDE shortcut registration requires explicit MISTY_LIVE_SHORTCUT_TESTS=1",
)
class TaskEscapeHelperTests(unittest.TestCase):
    def test_helper_accepts_bind_and_unbind_without_exiting(self):
        qt_application()
        process = QProcess()
        process.setProcessEnvironment(_env_with_shortcut("F8"))
        process.start(str(HELPER), [])
        self.assertTrue(process.waitForStarted(5000))
        self.assertTrue(process.waitForReadyRead(5000))
        process.write(b"CANCEL_ON\n")
        process.write(b"CANCEL_OFF\n")
        process.waitForBytesWritten(2000)
        self.assertFalse(process.waitForFinished(1000))
        self.assertEqual(process.state(), QProcess.ProcessState.Running)
        process.kill()
        process.waitForFinished(2000)


class CancelReleasesApprovalTests(unittest.TestCase):
    def test_cancel_denies_pending_approval_and_unblocks_waiter(self):
        broker = ConfirmationBroker(timeout=30)
        pending = broker.begin("desktop_task", {}, "test")
        self.assertIsNotNone(pending)
        outcome = {}

        waiter = threading.Thread(target=lambda: outcome.update(approved=broker.wait(pending)))
        waiter.start()
        started = time.monotonic()

        cancel_event = threading.Event()
        controller = VoiceController.__new__(VoiceController)
        controller._confirmation_broker = broker
        controller._recording = False
        controller._confirmation_recording = False
        controller._busy = True
        controller._agent_busy = True
        controller._voice_cancel_event = None
        controller.confirmation_resolved = _Signal()
        controller.response_changed = _Signal()
        controller._task_state_lock = threading.RLock
        controller._session_state = lambda name, default=None: (
            cancel_event if name == "_task_cancel_event" else default
        )
        self.assertTrue(controller.cancel_active_request())

        waiter.join(timeout=5)
        self.assertFalse(waiter.is_alive())
        self.assertFalse(outcome["approved"])
        self.assertLess(time.monotonic() - started, 5)


class _Signal:
    def emit(self, *_args):
        pass


def _env_with_shortcut(sequence):
    from PySide6.QtCore import QProcessEnvironment

    environment = QProcessEnvironment.systemEnvironment()
    environment.insert("MISTY_SHORTCUT", sequence)
    return environment


if __name__ == "__main__":
    unittest.main()
