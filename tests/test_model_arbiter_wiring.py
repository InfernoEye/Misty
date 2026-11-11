"""The model arbiter is wired into the controller, not just available.

Misty runs the reasoning model, the vision model and whisper.cpp one at a
time. ``app.brain.model_lease`` decides which may be loaded; these tests pin
the controller side: the callbacks it registers, and the ownership rule that
keeps another live request's model alive.
"""

import os
import threading
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject  # noqa: E402

from app.engine.voice_controller import VoiceController  # noqa: E402


class FakeAgent:
    """Stands in for MistyAgent's process lifecycle."""

    def __init__(self, fail_start=False, fail_stop=False):
        self.process = None
        self.starts = 0
        self.stops = 0
        self.fail_start = fail_start
        self.fail_stop = fail_stop

    def start(self):
        self.starts += 1
        if self.fail_start:
            raise RuntimeError("cannot start")
        self.process = object()

    def stop(self):
        self.stops += 1
        if self.fail_stop:
            raise RuntimeError("cannot stop")
        self.process = None


def build(agent=None):
    """A controller with only the state the arbiter needs."""
    controller = VoiceController.__new__(VoiceController)
    QObject.__init__(controller)
    controller._start_error_thread = None
    controller._worker = None
    controller._agent = agent
    return controller


class RegistrationTests(unittest.TestCase):
    def test_the_reasoning_model_is_registered(self):
        controller = build()

        self.assertIn(
            "reasoning", controller.model_arbiter().registered_resources()
        )

    def test_the_arbiter_is_created_once(self):
        controller = build()

        self.assertIs(controller.model_arbiter(), controller.model_arbiter())

    def test_state_is_json_safe(self):
        import json

        controller = build()
        controller.model_arbiter()

        state = controller.model_state()

        json.dumps(state)
        self.assertEqual(state["state"], "idle")
        self.assertEqual(state["registered"], ["reasoning"])

    def test_a_controller_without_a_model_reports_no_state(self):
        self.assertEqual(build_modelless().model_state(), {})


def build_modelless():
    """A controller that never touched the arbiter."""
    controller = VoiceController.__new__(VoiceController)
    QObject.__init__(controller)
    return controller


class AcquireReleaseTests(unittest.TestCase):
    def test_acquire_loads_and_release_unloads(self):
        agent = FakeAgent()
        controller = build(agent)

        result = controller.hold_model("reasoning")

        self.assertTrue(result.ok)
        self.assertEqual(agent.starts, 1)
        self.assertEqual(
            controller.model_arbiter().loaded_resources(), ("reasoning",)
        )

        self.assertTrue(result.release().ok)
        self.assertEqual(agent.stops, 1)
        self.assertEqual(controller.model_arbiter().loaded_resources(), ())

    def test_a_second_acquire_reloads_the_model(self):
        agent = FakeAgent()
        controller = build(agent)

        controller.hold_model("reasoning").release()
        controller.hold_model("reasoning")

        self.assertEqual(agent.starts, 2)

    def test_a_failed_load_is_reported_as_load_failed(self):
        controller = build(FakeAgent(fail_start=True))

        result = controller.hold_model("reasoning")

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "load_failed")
        self.assertEqual(controller.model_arbiter().loaded_resources(), ())

    def test_a_failed_unload_leaves_the_resource_blocked(self):
        agent = FakeAgent(fail_stop=True)
        controller = build(agent)

        result = controller.hold_model("reasoning")
        released = result.release()

        self.assertFalse(released.ok)
        self.assertEqual(released.code, "unload_failed")

    def test_an_unknown_resource_is_refused(self):
        self.assertEqual(build().hold_model("vision").code, "unknown_resource")

    def test_no_agent_means_no_load(self):
        self.assertEqual(build(None).hold_model("reasoning").code,
                         "load_failed")


class OwnershipTests(unittest.TestCase):
    """Another live request's model must never be unloaded behind its back."""

    def test_unload_refuses_while_another_request_owns_the_model(self):
        agent = FakeAgent()
        controller = build(agent)
        release = threading.Event()

        def hold():
            release.wait(5)

        other = threading.Thread(target=hold, daemon=True)
        other.start()
        # Cleanups run last-in-first-out: join first, then release, so the
        # thread is told to stop before we wait on it.
        self.addCleanup(other.join, 3)
        self.addCleanup(release.set)
        controller._worker = other

        self.assertFalse(controller._unload_reasoning_for_lease())
        self.assertEqual(agent.stops, 0)

    def test_unload_proceeds_when_the_caller_is_the_worker(self):
        agent = FakeAgent()
        controller = build(agent)
        controller._worker = threading.current_thread()

        self.assertTrue(controller._unload_reasoning_for_lease())
        self.assertEqual(agent.stops, 1)

    def test_unload_with_no_agent_is_a_no_op(self):
        self.assertTrue(build(None)._unload_reasoning_for_lease())


class OutOfBandUnloadTests(unittest.TestCase):
    """The vision/transcription hooks unload directly; the arbiter must know."""

    def test_releasing_a_held_token_clears_arbiter_state(self):
        controller = build(FakeAgent())
        controller.hold_model("reasoning")
        self.assertEqual(
            controller.model_arbiter().loaded_resources(), ("reasoning",)
        )

        controller._release_arbiter_leases("reasoning")

        self.assertEqual(controller.model_arbiter().loaded_resources(), ())

    def test_release_is_idempotent(self):
        controller = build(FakeAgent())
        controller.hold_model("reasoning")

        self.assertTrue(controller._release_arbiter_leases("reasoning"))
        self.assertFalse(controller._release_arbiter_leases("reasoning"))

    def test_unloading_without_the_arbiter_is_harmless(self):
        self.assertFalse(build_modelless()._release_arbiter_leases("reasoning"))


if __name__ == "__main__":
    unittest.main()
