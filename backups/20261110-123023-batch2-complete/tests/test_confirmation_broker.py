import unittest

from app.engine.confirmations import ConfirmationBroker


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ConfirmationBrokerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.broker = ConfirmationBroker(timeout=60, clock=self.clock)

    def test_only_one_request_can_be_pending(self):
        first = self.broker.begin("close_window", {"window_id": "1"}, "Close")
        self.assertIsNotNone(first)
        self.assertIsNone(self.broker.begin("close_window", {}, "Again"))
        self.assertEqual(
            self.broker.current_request_id(), first["request_id"]
        )

    def test_mismatched_request_id_cannot_approve(self):
        pending = self.broker.begin("close_window", {}, "Close")

        self.assertFalse(self.broker.resolve(True, request_id="other"))
        self.assertTrue(self.broker.is_pending())
        self.assertTrue(self.broker.resolve(True, request_id=pending["request_id"]))
        self.assertTrue(self.broker.wait(pending))
        self.assertFalse(self.broker.is_pending())

    def test_denial_is_not_approval(self):
        pending = self.broker.begin("close_window", {}, "Close")

        self.assertTrue(self.broker.resolve(False, request_id=pending["request_id"]))
        self.assertFalse(self.broker.wait(pending))

    def test_expired_request_cannot_be_approved(self):
        pending = self.broker.begin("close_window", {}, "Close")
        self.clock.advance(61)

        self.assertFalse(self.broker.resolve(True, request_id=pending["request_id"]))
        self.assertFalse(self.broker.wait(pending))
        self.assertFalse(self.broker.is_pending())

    def test_wait_times_out_without_decision(self):
        broker = ConfirmationBroker(timeout=0.01, clock=self.clock)
        pending = broker.begin("write_clipboard", {}, "Write")

        self.assertFalse(broker.wait(pending))
        self.assertFalse(broker.is_pending())

    def test_late_response_after_resolution_is_ignored(self):
        pending = self.broker.begin("forget_memory", {}, "Forget")

        self.assertTrue(self.broker.resolve(False, request_id=pending["request_id"]))
        self.assertFalse(
            self.broker.resolve(True, request_id=pending["request_id"])
        )


if __name__ == "__main__":
    unittest.main()
