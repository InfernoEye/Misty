"""Tests for the isolated model-resource lease/arbitration module.

Only fake callbacks are used: no model, process, network or desktop state is
touched.  Threads are joined and every gate is opened in a ``finally`` so the
suite cannot hang.
"""

import ast
import json
import threading
import time
import unittest
from pathlib import Path

from app.brain.model_lease import (
    DEFAULT_TIMEOUT,
    LEASE_CODES,
    STATE_NAMES,
    LeaseError,
    LeaseResult,
    LeaseToken,
    ModelLease,
    ModelLeaseError,
    plan_switch,
)


MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "brain" / "model_lease.py"
)

SECRET = "SECRET-7c1e3a-unload-transcript"

RESOURCES = ("reasoning", "vision", "whisper")


class Recorder:
    """Records callback order and can make callbacks fail or block.

    Failure modes per ``(kind, name)``: ``"raise"`` raises a RuntimeError
    whose text is ``SECRET``, ``"false"`` returns False, ``"none"`` returns
    None.  A gate blocks the callback until the event is set; ``started`` is
    set as soon as the callback is entered.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.events = []
        self.fail = {}
        self.gate = {}
        self.started = {}
        self.calls = 0

    def run(self, kind, name):
        with self._lock:
            self.events.append(f"{kind}:{name}")
            self.calls += 1
        started = self.started.get((kind, name))
        if started is not None:
            started.set()
        gate = self.gate.get((kind, name))
        if gate is not None:
            gate.wait(5.0)
        mode = self.fail.get((kind, name))
        if mode == "raise":
            raise RuntimeError(SECRET)
        if mode == "false":
            return False
        if mode == "none":
            return None
        return True

    def loader(self, name):
        return lambda: self.run("load", name)

    def unloader(self, name):
        return lambda: self.run("unload", name)

    def register(self, arbiter, *names):
        for name in names:
            arbiter.register(name, self.loader(name), self.unloader(name))

    def order(self):
        with self._lock:
            return list(self.events)


class LeaseTestCase(unittest.TestCase):
    def make(self, *names):
        arbiter = ModelLease()
        recorder = Recorder()
        recorder.register(arbiter, *(names or RESOURCES))
        return arbiter, recorder

    def assertNoSecret(self, *values):
        for value in values:
            self.assertNotIn(SECRET, json.dumps(value, default=str))


class AcquireAndSwitchTests(LeaseTestCase):
    def test_acquire_unloads_before_loading_next_resource(self):
        arbiter, recorder = self.make("reasoning", "vision", "whisper")

        first = arbiter.acquire("reasoning")
        self.assertTrue(first.ok)
        self.assertEqual(first.code, "loaded")
        self.assertEqual(first.token.resource, "reasoning")
        self.assertEqual(arbiter.loaded_resources(), ("reasoning",))
        self.assertEqual(arbiter.state()["state"], "loaded")

        released = arbiter.release(first.token)
        self.assertTrue(released.ok)
        self.assertEqual(released.code, "released")
        self.assertEqual(arbiter.loaded_resources(), ())

        second = arbiter.acquire("vision")
        self.assertTrue(second.ok)
        self.assertEqual(arbiter.loaded_resources(), ("vision",))
        self.assertEqual(
            recorder.order(),
            ["load:reasoning", "unload:reasoning", "load:vision"],
        )

        arbiter.release(second.token)
        self.assertEqual(recorder.order()[-1], "unload:vision")
        self.assertEqual(arbiter.loaded_resources(), ())
        self.assertEqual(arbiter.state()["state"], "idle")

    def test_switch_unloads_still_loaded_resource_before_target_load(self):
        arbiter, recorder = self.make("reasoning", "vision")

        first = arbiter.acquire("reasoning")
        recorder.fail[("unload", "reasoning")] = "false"
        self.assertFalse(arbiter.release(first.token).ok)
        self.assertEqual(arbiter.state()["state"], "blocked")

        del recorder.fail[("unload", "reasoning")]
        second = arbiter.acquire("vision")
        self.assertTrue(second.ok)
        self.assertEqual(second.unloaded, ("reasoning",))
        self.assertEqual(
            recorder.order(),
            [
                "load:reasoning",
                "unload:reasoning",
                "unload:reasoning",
                "load:vision",
            ],
        )
        self.assertEqual(arbiter.loaded_resources(), ("vision",))
        arbiter.release(second.token)

    def test_already_loaded_resource_is_reacquired_without_load(self):
        arbiter, recorder = self.make("reasoning", "vision")

        # Leave a resource loaded but unleased via the explicit restore policy.
        first = arbiter.acquire("reasoning")
        recorder.fail[("unload", "reasoning")] = "false"
        arbiter.release(first.token)
        del recorder.fail[("unload", "reasoning")]
        self.assertEqual(arbiter.state()["state"], "blocked")

        loads_before = recorder.order().count("load:reasoning")
        result = arbiter.acquire("reasoning")
        self.assertTrue(result.ok)
        self.assertEqual(result.code, "already_loaded")
        self.assertEqual(recorder.order().count("load:reasoning"), loads_before)
        arbiter.release(result.token)

    def test_restore_is_opt_in(self):
        for restore, expected_loaded in ((False, None), (True, "reasoning")):
            with self.subTest(restore=restore):
                arbiter, recorder = self.make("reasoning", "vision")
                first = arbiter.acquire("reasoning")
                recorder.fail[("unload", "reasoning")] = "false"
                arbiter.release(first.token)
                del recorder.fail[("unload", "reasoning")]

                second = arbiter.acquire("vision", restore=restore)
                self.assertTrue(second.ok)
                released = arbiter.release(second.token)
                self.assertTrue(released.ok)
                self.assertEqual(released.restored, expected_loaded)
                snapshot = arbiter.state()
                self.assertEqual(snapshot["loaded"], expected_loaded)
                if expected_loaded is None:
                    self.assertEqual(snapshot["state"], "idle")
                else:
                    self.assertEqual(snapshot["state"], "loaded")
                    self.assertEqual(
                        recorder.order()[-1], "load:reasoning"
                    )
                    # Loaded but unleased: a later acquire must not reload.
                    again = arbiter.acquire("reasoning")
                    self.assertEqual(again.code, "already_loaded")
                    arbiter.release(again.token)


class ContextManagerTests(LeaseTestCase):
    def test_lease_context_manager_loads_and_releases(self):
        arbiter, recorder = self.make("reasoning", "vision")

        with arbiter.lease("vision") as result:
            self.assertIsInstance(result, LeaseResult)
            self.assertTrue(result.ok)
            self.assertEqual(arbiter.loaded_resources(), ("vision",))
            self.assertEqual(arbiter.state()["state"], "loaded")

        self.assertEqual(arbiter.loaded_resources(), ())
        self.assertEqual(recorder.order(), ["load:vision", "unload:vision"])
        self.assertEqual(arbiter.state()["state"], "idle")

    def test_acquire_result_is_a_context_manager(self):
        arbiter, recorder = self.make("vision")

        with arbiter.acquire("vision") as result:
            self.assertTrue(result.ok)
            self.assertEqual(arbiter.loaded_resources(), ("vision",))

        self.assertEqual(arbiter.loaded_resources(), ())
        self.assertEqual(recorder.order(), ["load:vision", "unload:vision"])

    def test_release_is_idempotent_after_context_exit(self):
        arbiter, _ = self.make("vision")
        with arbiter.lease("vision") as result:
            token = result.token
        second = arbiter.release(token)
        self.assertFalse(second.ok)
        self.assertEqual(second.code, "already_released")

    def test_unload_failure_on_clean_exit_is_raised(self):
        arbiter, recorder = self.make("vision")
        recorder.fail[("unload", "vision")] = "raise"

        with self.assertRaises(ModelLeaseError) as caught:
            with arbiter.lease("vision"):
                pass

        self.assertEqual(caught.exception.code, "unload_failed")
        self.assertEqual(arbiter.loaded_resources(), ("vision",))
        self.assertEqual(arbiter.state()["state"], "blocked")
        self.assertNoSecret(str(caught.exception), arbiter.state())

    def test_body_exception_wins_and_unload_failure_stays_visible(self):
        arbiter, recorder = self.make("vision")
        recorder.fail[("unload", "vision")] = "false"

        with self.assertRaises(KeyError):
            with arbiter.lease("vision"):
                raise KeyError("body failure")

        snapshot = arbiter.state()
        self.assertEqual(snapshot["loaded"], "vision")
        self.assertEqual(snapshot["last_error"]["code"], "unload_failed")

    def test_lease_timeout_raises_structured_error(self):
        arbiter, _ = self.make("reasoning", "vision")
        holder_ready = threading.Event()
        allow_release = threading.Event()
        held = []

        def holder():
            result = arbiter.acquire("reasoning", timeout=2.0)
            held.append(result)
            holder_ready.set()
            allow_release.wait(5.0)
            arbiter.release(result.token)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        try:
            self.assertTrue(holder_ready.wait(2.0))
            with self.assertRaises(ModelLeaseError) as caught:
                with arbiter.lease("reasoning", timeout=0.05):
                    pass
            self.assertEqual(caught.exception.code, "timeout")
        finally:
            allow_release.set()
            thread.join(5.0)

        self.assertFalse(thread.is_alive())
        self.assertTrue(held[0].ok)
        self.assertEqual(arbiter.loaded_resources(), ())


class FailureTests(LeaseTestCase):
    def test_load_failure_leaves_nothing_loaded(self):
        for mode in ("raise", "false", "none"):
            with self.subTest(mode=mode):
                arbiter, recorder = self.make("reasoning", "vision")
                recorder.fail[("load", "vision")] = mode

                result = arbiter.acquire("vision")
                self.assertFalse(result.ok)
                self.assertEqual(result.code, "load_failed")
                self.assertIsNone(result.token)
                self.assertEqual(arbiter.loaded_resources(), ())
                snapshot = arbiter.state()
                self.assertEqual(snapshot["state"], "idle")
                self.assertEqual(snapshot["loaded"], None)
                self.assertEqual(snapshot["leases"], [])
                self.assertEqual(snapshot["last_error"]["code"], "load_failed")
                self.assertNoSecret(snapshot, result.message)

                # A later, healthy load works normally.
                del recorder.fail[("load", "vision")]
                retry = arbiter.acquire("vision")
                self.assertTrue(retry.ok)
                arbiter.release(retry.token)
                self.assertEqual(arbiter.loaded_resources(), ())

    def test_unload_failure_blocks_new_load_and_state_stays_honest(self):
        arbiter, recorder = self.make("reasoning", "vision")
        first = arbiter.acquire("reasoning")
        recorder.fail[("unload", "reasoning")] = "false"

        released = arbiter.release(first.token)
        self.assertFalse(released.ok)
        self.assertEqual(released.code, "unload_failed")
        self.assertTrue(released.loaded)

        snapshot = arbiter.state()
        self.assertEqual(snapshot["loaded"], "reasoning")
        self.assertEqual(snapshot["loaded_resources"], ["reasoning"])
        self.assertEqual(snapshot["state"], "blocked")
        self.assertEqual(snapshot["last_error"]["code"], "unload_failed")

        blocked = arbiter.acquire("vision", timeout=0.5)
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.code, "unload_failed")
        self.assertNotIn("load:vision", recorder.order())
        self.assertEqual(arbiter.loaded_resources(), ("reasoning",))
        self.assertEqual(arbiter.state()["state"], "blocked")

        # Recovery: once unloading works, the switch completes and state clears.
        del recorder.fail[("unload", "reasoning")]
        recovered = arbiter.acquire("vision")
        self.assertTrue(recovered.ok)
        self.assertEqual(arbiter.state()["last_error"], None)
        arbiter.release(recovered.token)

    def test_callback_exceptions_become_fixed_messages(self):
        arbiter, recorder = self.make("vision")
        recorder.fail[("load", "vision")] = "raise"

        result = arbiter.acquire("vision")
        self.assertEqual(result.code, "load_failed")
        self.assertEqual(result.message, "The model resource could not be loaded.")
        self.assertNoSecret(
            result.message, result.to_dict(), repr(result), arbiter.state()
        )

        recorder.fail[("load", "vision")] = "raise"
        with self.assertRaises(ModelLeaseError) as caught:
            with arbiter.lease("vision"):
                pass
        self.assertEqual(caught.exception.code, "load_failed")
        self.assertNoSecret(str(caught.exception))

    def test_unknown_resource(self):
        arbiter, recorder = self.make("reasoning")
        result = arbiter.acquire("speech")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "unknown_resource")
        self.assertEqual(recorder.calls, 0)
        self.assertEqual(arbiter.registered_resources(), ("reasoning",))

    def test_invalid_names_raise(self):
        arbiter, _ = self.make("reasoning")
        for name in ("", "Vision", "1vision", "reasoning model", None, 3,
                     "a" * 65):
            with self.subTest(name=name):
                with self.assertRaises(LeaseError):
                    arbiter.acquire(name)

    def test_invalid_timeout_raises(self):
        arbiter, _ = self.make("reasoning")
        for timeout in (-0.1, "5", True, float("nan"), float("inf")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(LeaseError):
                    arbiter.acquire("reasoning", timeout=timeout)
        result = arbiter.acquire("reasoning", timeout=None)
        self.assertTrue(result.ok)
        arbiter.release(result.token)

    def test_release_rejects_foreign_and_forged_tokens(self):
        arbiter, _ = self.make("vision")
        with self.assertRaises(LeaseError):
            arbiter.release("vision")
        forged = LeaseToken(resource="vision", lease_id=999)
        result = arbiter.release(forged)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "invalid_token")
        self.assertEqual(arbiter.loaded_resources(), ())


class RegistrationTests(LeaseTestCase):
    def test_invalid_callbacks_are_rejected_without_calling_them(self):
        arbiter = ModelLease()
        recorder = Recorder()
        invalid_pairs = (
            (None, recorder.unloader("vision")),
            (recorder.loader("vision"), None),
            ("load", "unload"),
            (recorder.loader("vision"), 42),
            (None, None),
        )
        for load, unload in invalid_pairs:
            with self.subTest(load=load, unload=unload):
                self.assertFalse(
                    arbiter.register("vision", load, unload)
                )
        self.assertEqual(arbiter.registered_resources(), ())
        self.assertEqual(recorder.calls, 0)
        self.assertFalse(arbiter.register("vision", recorder.loader("vision"),
                                          recorder.unloader("vision"),
                                          replace="yes"))

    def test_duplicate_registration_is_rejected(self):
        arbiter, recorder = self.make("reasoning")
        self.assertFalse(
            arbiter.register(
                "reasoning", recorder.loader("reasoning"),
                recorder.unloader("reasoning"),
            )
        )
        self.assertTrue(
            arbiter.register(
                "reasoning", recorder.loader("reasoning"),
                recorder.unloader("reasoning"), replace=True,
            )
        )
        self.assertEqual(arbiter.registered_resources(), ("reasoning",))

    def test_cannot_replace_loaded_or_leased_resource(self):
        arbiter, recorder = self.make("reasoning")
        held = arbiter.acquire("reasoning")
        self.assertFalse(
            arbiter.register(
                "reasoning", recorder.loader("reasoning"),
                recorder.unloader("reasoning"), replace=True,
            )
        )
        arbiter.release(held.token)
        self.assertTrue(
            arbiter.register(
                "reasoning", recorder.loader("reasoning"),
                recorder.unloader("reasoning"), replace=True,
            )
        )

    def test_invalid_registration_names(self):
        arbiter, recorder = self.make()
        for name in ("", "Vision", None, 7, "x" * 65):
            with self.subTest(name=name):
                self.assertFalse(
                    arbiter.register(
                        name, recorder.loader("vision"),
                        recorder.unloader("vision"),
                    )
                )


class ConcurrencyTests(LeaseTestCase):
    def test_timeout_while_another_thread_holds_the_resource(self):
        arbiter, recorder = self.make("reasoning", "vision")
        holder_ready = threading.Event()
        allow_release = threading.Event()
        holder_result = []

        def holder():
            result = arbiter.acquire("reasoning", timeout=2.0)
            holder_result.append(result)
            holder_ready.set()
            allow_release.wait(5.0)
            arbiter.release(result.token)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        try:
            self.assertTrue(holder_ready.wait(2.0))
            self.assertEqual(arbiter.loaded_resources(), ("reasoning",))

            started = time.monotonic()
            same = arbiter.acquire("reasoning", timeout=0.05)
            elapsed = time.monotonic() - started
            self.assertFalse(same.ok)
            self.assertEqual(same.code, "timeout")
            self.assertLess(elapsed, 1.0)

            # A different model also waits for the busy holder instead of
            # unloading it from under the active lease.
            other = arbiter.acquire("vision", timeout=0.05)
            self.assertFalse(other.ok)
            self.assertEqual(other.code, "timeout")
            self.assertNotIn("load:vision", recorder.order())
            self.assertEqual(arbiter.state()["counters"]["timeouts"], 2)
        finally:
            allow_release.set()
            thread.join(5.0)

        self.assertFalse(thread.is_alive())
        self.assertTrue(holder_result[0].ok)
        self.assertEqual(arbiter.loaded_resources(), ())
        self.assertEqual(arbiter.state()["state"], "idle")

    def test_timeout_while_a_switch_is_running_but_state_stays_readable(self):
        arbiter, recorder = self.make("reasoning", "vision")
        gate = threading.Event()
        started = threading.Event()
        recorder.gate[("load", "reasoning")] = gate
        recorder.started[("load", "reasoning")] = started
        results = []

        thread = threading.Thread(
            target=lambda: results.append(
                arbiter.acquire("reasoning", timeout=5.0)
            ),
            daemon=True,
        )
        thread.start()
        try:
            self.assertTrue(started.wait(2.0))
            began = time.monotonic()
            snapshot = arbiter.state()
            elapsed = time.monotonic() - began
            self.assertLess(elapsed, 0.5)
            self.assertEqual(snapshot["state"], "loading")
            self.assertEqual(
                snapshot["switching"], {"from": None, "to": "reasoning"}
            )
            self.assertEqual(snapshot["loaded"], None)
            json.dumps(snapshot, allow_nan=False)

            other = arbiter.acquire("vision", timeout=0.05)
            self.assertFalse(other.ok)
            self.assertEqual(other.code, "timeout")
            self.assertEqual(len(results), 0)
        finally:
            gate.set()
            thread.join(5.0)

        self.assertFalse(thread.is_alive())
        self.assertTrue(results[0].ok)
        arbiter.release(results[0].token)
        self.assertEqual(arbiter.loaded_resources(), ())

    def test_concurrent_acquire_is_serialized_and_keeps_one_loaded(self):
        names = ("reasoning", "vision", "whisper")
        arbiter, recorder = self.make(*names)
        start = threading.Event()
        errors = []
        successes = []

        def worker(index):
            start.wait(2.0)
            try:
                for step in range(3):
                    name = names[(index + step) % len(names)]
                    with arbiter.lease(name, timeout=10.0) as result:
                        if not result.ok:
                            errors.append(result.code)
                            continue
                        snapshot = arbiter.state()
                        if snapshot["state"] != "loaded":
                            errors.append(snapshot["state"])
                        if snapshot["loaded"] != name:
                            errors.append("wrong-loaded")
                        if len(arbiter.loaded_resources()) != 1:
                            errors.append("not-exclusive")
                        successes.append(name)
                        time.sleep(0.003)
            except Exception as error:  # pragma: no cover - failure path
                errors.append(repr(error))

        threads = [
            threading.Thread(target=worker, args=(index,), daemon=True)
            for index in range(4)
        ]
        for thread in threads:
            thread.start()
        start.set()
        for thread in threads:
            thread.join(15.0)

        self.assertEqual([t for t in threads if t.is_alive()], [])
        self.assertEqual(errors, [])
        self.assertEqual(len(successes), 12)
        self.assertEqual(arbiter.loaded_resources(), ())
        counters = arbiter.state()["counters"]
        self.assertEqual(counters["loads"], 12)
        self.assertEqual(counters["unloads"], 12)
        self.assertEqual(counters["acquired"], 12)
        self.assertEqual(counters["timeouts"], 0)
        self.assertEqual(counters["load_failures"], 0)
        self.assertEqual(counters["unload_failures"], 0)

    def test_reentrant_acquire_is_rejected(self):
        arbiter, recorder = self.make("reasoning", "vision")

        with arbiter.lease("reasoning"):
            nested = arbiter.acquire("reasoning", timeout=0)
            self.assertFalse(nested.ok)
            self.assertEqual(nested.code, "reentrant")
            cross = arbiter.acquire("vision", timeout=0)
            self.assertFalse(cross.ok)
            self.assertEqual(cross.code, "lease_held")

        self.assertEqual(
            recorder.order(),
            ["load:reasoning", "unload:reasoning"],
        )
        self.assertEqual(arbiter.loaded_resources(), ())


class DiagnosticsTests(LeaseTestCase):
    def test_state_is_json_safe_and_deterministic(self):
        arbiter, recorder = self.make("whisper", "reasoning", "vision")
        first = arbiter.acquire("reasoning")

        snapshot = arbiter.state()
        blob = json.dumps(snapshot, allow_nan=False, sort_keys=True)
        self.assertEqual(json.loads(blob), snapshot)
        self.assertIn(snapshot["state"], STATE_NAMES)
        self.assertEqual(snapshot["registered"], ["reasoning", "vision",
                                                   "whisper"])
        self.assertEqual(snapshot["loaded"], "reasoning")
        self.assertEqual(snapshot["loaded_resources"], ["reasoning"])
        self.assertEqual(
            snapshot["leases"],
            [{"resource": "reasoning",
              "lease_id": first.token.lease_id,
              "thread": first.token.thread_ident}],
        )
        self.assertIsNone(snapshot["switching"])
        self.assertIsNone(snapshot["last_error"])
        self.assertEqual(
            sorted(snapshot["counters"]),
            ["acquired", "load_failures", "loads", "rejected", "released",
             "timeouts", "unload_failures", "unloads"],
        )

        # The snapshot is a copy: mutating it cannot corrupt the arbiter.
        snapshot["counters"]["loads"] = 999
        snapshot["leases"].clear()
        self.assertEqual(arbiter.state()["counters"]["loads"], 1)
        self.assertEqual(len(arbiter.state()["leases"]), 1)

        arbiter.release(first.token)
        final = arbiter.state()
        json.dumps(final, allow_nan=False)
        self.assertEqual(final["state"], "idle")
        self.assertEqual(final["loaded_resources"], [])
        self.assertEqual(final["leases"], [])

        result = arbiter.acquire("vision")
        self.assertIn(result.code, LEASE_CODES)
        self.assertEqual(result.to_dict()["resource"], "vision")
        json.dumps(result.to_dict(), allow_nan=False)
        arbiter.release(result.token)

    def test_loaded_resources_serialises_as_array(self):
        arbiter, _ = self.make("vision")
        self.assertEqual(list(arbiter.loaded_resources()), [])
        result = arbiter.acquire("vision")
        self.assertEqual(
            json.loads(json.dumps(arbiter.loaded_resources())), ["vision"]
        )
        arbiter.release(result.token)

    def test_plan_switch_is_pure_and_json_safe(self):
        arbiter, recorder = self.make("reasoning")
        before = arbiter.state()

        cold = plan_switch(None, "reasoning")
        self.assertEqual(cold["steps"], ["load_target"])
        self.assertTrue(cold["valid"])
        self.assertTrue(cold["needed"])

        switch = plan_switch("reasoning", "vision")
        self.assertEqual(switch["steps"], ["unload_current", "load_target"])

        same = plan_switch("vision", "vision")
        self.assertEqual(same["steps"], [])
        self.assertFalse(same["needed"])

        shutdown = plan_switch("vision", None)
        self.assertFalse(shutdown["valid"])
        self.assertFalse(shutdown["needed"])
        self.assertEqual(plan_switch("Bad Name", "vision")["valid"], False)

        for plan in (cold, switch, same, shutdown):
            json.dumps(plan, allow_nan=False)

        self.assertEqual(arbiter.state(), before)
        self.assertEqual(recorder.calls, 0)


class IsolationTests(unittest.TestCase):
    FORBIDDEN_IMPORTS = {
        "subprocess", "os", "sys", "socket", "urllib", "http", "requests",
        "logging", "PySide6", "PyQt6", "PyQt5", "asyncio", "multiprocessing",
        "app",
    }
    ALLOWED_IMPORTS = {
        "collections", "contextlib", "dataclasses", "math", "re", "threading",
        "time",
    }

    @classmethod
    def setUpClass(cls):
        cls.source = MODULE_PATH.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_module_imports_only_pure_stdlib(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & self.FORBIDDEN_IMPORTS, set())
        self.assertLessEqual(imported, self.ALLOWED_IMPORTS)

    def test_module_has_no_process_execution_or_io_calls(self):
        forbidden_calls = {
            "open", "exec", "eval", "compile", "__import__", "system",
            "popen", "Popen", "run", "check_output", "spawn", "fork",
        }
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, forbidden_calls)
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, {"system", "popen", "spawn"})

    def test_module_carries_no_paths_or_content_constants(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                lowered = node.value.lower()
                for marker in (".gguf", ".bin", ".wav", "http", ".onnx",
                               ".safetensors"):
                    self.assertNotIn(marker, lowered)


if __name__ == "__main__":
    unittest.main()
