import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import app.brain.agent as agent_module
from app.brain.agent import MistyAgent
import app.desktop.ydotool_backend as backend_module
from app.desktop.ydotool_backend import YdotoolBackend
from tests.agent_fakes import (
    FakeRegistry,
    TestableMistyAgent,
    plan_response,
    plan_step,
)


class StubProcess:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return None if not self.terminated else 0

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


class CountingAgent(MistyAgent):
    def __init__(self):
        super().__init__(registry=mock.Mock())
        self.stop_calls = 0

    def start(self):
        self.process = StubProcess()

    def stop(self):
        self.stop_calls += 1
        super().stop()


class ModelHoldTests(unittest.TestCase):
    def setUp(self):
        self.agent = CountingAgent()
        self.agent.start()

    def tearDown(self):
        self.agent._cancel_idle_timer()

    def test_run_inside_a_held_task_keeps_the_model_loaded(self):
        registry = FakeRegistry()
        response = plan_response([plan_step("step-1", "get_time")])
        agent = TestableMistyAgent(registry, [response, response])
        with agent.hold():
            agent.run("What time is it")
            self.assertFalse(agent.stopped)
            self.assertTrue(agent.task_held)
        self.assertFalse(agent.task_held)

    def test_run_without_a_hold_still_unloads_the_model(self):
        response = plan_response([plan_step("step-1", "get_time")])
        agent = TestableMistyAgent(FakeRegistry(), [response, response])
        agent.run("What time is it")
        self.assertTrue(agent.stopped)

    def test_model_unloads_after_idle_timeout_when_task_ends(self):
        with mock.patch.object(agent_module, "IDLE_UNLOAD_SECONDS", 0.2):
            with self.agent.hold():
                pass
            self.assertEqual(self.agent.stop_calls, 0)
            time.sleep(0.6)
            self.assertEqual(self.agent.stop_calls, 1)

    def test_new_task_before_timeout_cancels_the_unload(self):
        with mock.patch.object(agent_module, "IDLE_UNLOAD_SECONDS", 0.5):
            with self.agent.hold():
                pass
            with self.agent.hold():
                time.sleep(0.8)
                self.assertEqual(self.agent.stop_calls, 0)
            time.sleep(0.9)
            self.assertEqual(self.agent.stop_calls, 1)

    def test_idle_timeout_is_thirty_seconds(self):
        self.assertEqual(agent_module.IDLE_UNLOAD_SECONDS, 30)


class DesktopErrorLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "misty-desktop.log"

    def tearDown(self):
        self.tmp.cleanup()

    def backend(self, runner):
        return YdotoolBackend(runner=runner, log_path=self.log)

    def test_failed_command_records_exit_code_and_stderr(self):
        def runner(argv, input, timeout):
            return subprocess.CompletedProcess(
                argv, 1, stdout=b"", stderr=b"ydotool: cannot connect to socket"
            )

        backend = self.backend(runner)
        backend._exec(["/usr/bin/ydotool", "click", "0xC0"])
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("ydotool exited 1: ydotool: cannot connect to socket", text)

    def test_timeout_is_recorded(self):
        def runner(argv, input, timeout):
            raise subprocess.TimeoutExpired(argv, timeout)

        backend = self.backend(runner)
        backend._exec(["/usr/bin/ydotool", "mousemove"])
        self.assertIn("ydotool timed out", self.log.read_text(encoding="utf-8"))

    def test_successful_command_writes_nothing(self):
        def runner(argv, input, timeout):
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

        backend = self.backend(runner)
        self.assertIsNone(backend._exec(["/usr/bin/ydotool", "key", "28:1"]))
        self.assertFalse(self.log.exists())

    def test_typed_text_never_reaches_the_log(self):
        def runner(argv, input, timeout):
            return subprocess.CompletedProcess(
                argv, 2, stdout=b"", stderr=b"bad argument"
            )

        backend = self.backend(runner)
        backend._exec(["/usr/bin/ydotool", "type", "--file", "-"], b"secret words")
        self.assertNotIn("secret words", self.log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
