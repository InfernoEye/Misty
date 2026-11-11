"""The bounded observe → act → check loop and its submit guard.

Nothing here touches a real screen, model or mouse: observe/decide/act are
fakes, which is the point — the loop's safety properties are structural
(limits, stalls, one action per round, no purchase clicks) and testable
without them.
"""

import unittest

from app.brain.agent_loop import (
    DECIDE_ACT,
    DEFAULT_MAX_ROUNDS,
    LoopReport,
    run_loop,
)
from app.brain.tools import is_submit_target


def scripted(decisions, results=None):
    """Build decide/act/observe fakes from a script."""
    calls = []
    queue = list(decisions)
    results = list(results or [])

    def decide(observation, history):
        calls.append(("decide", observation))
        if not queue:
            return {"kind": "done"}
        return queue.pop(0)

    def act(tool, arguments):
        calls.append(("act", tool, arguments))
        if results:
            return results.pop(0)
        return {"success": True, "message": "ok"}

    return decide, act, calls


class BoundsTests(unittest.TestCase):
    def test_the_round_limit_stops_the_loop(self):
        decide, act, calls = scripted(
            [{"kind": DECIDE_ACT, "tool": "get_time", "arguments": {}}] * 20,
        )
        report = run_loop(
            lambda: f"screen-{len(calls)}", decide, act, max_rounds=3
        )
        self.assertEqual(report.status, "rounds")
        self.assertEqual(len(report.actions), 3)

    def test_a_done_decision_ends_cleanly(self):
        decide, act, _ = scripted([
            {"kind": DECIDE_ACT, "tool": "get_time", "arguments": {}},
            {"kind": "done", "note": "finished"},
        ])
        report = run_loop(lambda: "s", decide, act)
        self.assertEqual(report.status, "done")
        self.assertEqual(len(report.actions), 1)
        self.assertIn("Done in 1", report.summary())

    def test_an_ask_decision_surfaces_the_question(self):
        decide, act, _ = scripted([
            {"kind": "ask", "note": "Which product do you mean?"}
        ])
        report = run_loop(lambda: "s", decide, act)
        self.assertEqual(report.status, "ask")
        self.assertEqual(report.note, "Which product do you mean?")
        self.assertEqual(report.actions, [])

    def test_the_time_budget_stops_the_loop(self):
        now = [0.0]

        def clock():
            now[0] += 100.0
            return now[0]

        decide, act, _ = scripted(
            [{"kind": DECIDE_ACT, "tool": "get_time", "arguments": {}}] * 10
        )
        report = run_loop(
            lambda: "changing", decide, act, budget_seconds=1.0, clock=clock
        )
        self.assertEqual(report.status, "budget")

    def test_a_stalled_screen_stops_the_loop(self):
        # Same observation every round: repeating the click is the danger.
        decide, act, calls = scripted(
            [{"kind": DECIDE_ACT, "tool": "desktop_click",
              "arguments": {"x": 1, "y": 2}}] * 10
        )
        report = run_loop(lambda: "identical screen", decide, act,
                          stall_limit=2)
        self.assertEqual(report.status, "failed")
        self.assertIn("stopped changing", report.note)
        self.assertLess(len(report.actions), 5)


class FailureTests(unittest.TestCase):
    def test_one_failed_action_ends_the_loop(self):
        decide, act, _ = scripted(
            [{"kind": DECIDE_ACT, "tool": "desktop_click",
              "arguments": {"x": 1, "y": 1}}] * 5,
            results=[{"success": False, "message": "no input backend"}],
        )
        report = run_loop(lambda: "screen", decide, act)
        self.assertEqual(report.status, "failed")
        self.assertIn("no input backend", report.note)
        self.assertEqual(len(report.actions), 1)

    def test_a_declined_confirmation_stops_as_a_question(self):
        decide, act, _ = scripted(
            [{"kind": DECIDE_ACT, "tool": "desktop_click",
              "arguments": {"x": 1, "y": 1}}],
            results=[{"success": False, "code": "confirmation_declined"}],
        )
        report = run_loop(lambda: "screen", decide, act)
        self.assertEqual(report.status, "ask")

    def test_a_decider_exception_stops_the_loop(self):
        def decide(observation, history):
            raise RuntimeError("model died")

        report = run_loop(lambda: "s", decide,
                          lambda tool, args: {"success": True})
        self.assertEqual(report.status, "failed")
        self.assertIn("could not decide", report.note.lower())

    def test_an_observer_exception_stops_the_loop(self):
        def observe():
            raise RuntimeError("capture failed")

        report = run_loop(observe, lambda o, h: {"kind": "done"},
                          lambda tool, args: {"success": True})
        self.assertEqual(report.status, "failed")

    def test_malformed_decisions_are_rejected(self):
        for decision in (
            "nope",
            {},
            {"kind": "fly"},
            {"kind": DECIDE_ACT},
            {"kind": DECIDE_ACT, "tool": "get_time", "arguments": "x"},
        ):
            with self.subTest(decision=decision):
                report = run_loop(
                    lambda: "s", lambda o, h, d=decision: d,
                    lambda tool, args: {"success": True},
                )
                self.assertEqual(report.status, "failed")

    def test_a_raising_action_stops_the_loop(self):
        def act(tool, arguments):
            raise RuntimeError("boom")

        report = run_loop(
            lambda: "s",
            lambda o, h: {"kind": DECIDE_ACT, "tool": "desktop_click",
                          "arguments": {"x": 1, "y": 1}},
            act,
        )
        self.assertEqual(report.status, "failed")

    def test_a_reporter_exception_never_stops_the_work(self):
        def on_step(step):
            raise RuntimeError("ui gone")

        # The observation changes each round, so the stall guard does not
        # fire: this test is about the reporter only.
        counter = {"n": 0}

        def observe():
            counter["n"] += 1
            return f"screen {counter['n']}"

        report = run_loop(
            observe,
            lambda o, h: {"kind": DECIDE_ACT, "tool": "get_time",
                          "arguments": {}},
            lambda tool, args: {"success": True},
            on_step=on_step,
        )
        self.assertIn(report.status, ("rounds", "done"))

    def test_non_callables_are_refused(self):
        with self.assertRaises(TypeError):
            run_loop(None, None, None)


class ReportTests(unittest.TestCase):
    def test_arguments_are_redacted_by_default(self):
        decide, act, _ = scripted([
            {"kind": DECIDE_ACT, "tool": "desktop_type_text",
             "arguments": {"text": "PRIVATE-CANARY"}},
            {"kind": "done"},
        ])
        report = run_loop(lambda: "screen", decide, act)
        rendered = str(report.to_dict())
        self.assertNotIn("PRIVATE-CANARY", rendered)
        self.assertIn("desktop_type_text", rendered)

    def test_the_full_arguments_are_available_on_request(self):
        decide, act, _ = scripted([
            {"kind": DECIDE_ACT, "tool": "desktop_type_text",
             "arguments": {"text": "hello"}},
            {"kind": "done"},
        ])
        report = run_loop(lambda: "screen", decide, act)
        step = report.actions[0]
        self.assertEqual(step.to_dict(redact=False)["arguments"],
                         {"text": "hello"})

    def test_summaries_are_honest_for_each_status(self):
        for status in ("done", "ask", "budget", "rounds", "failed"):
            with self.subTest(status=status):
                text = LoopReport(status, [], note="").summary()
                self.assertTrue(text)
                self.assertNotIn("success", text.lower())


class SubmitGuardTests(unittest.TestCase):
    def test_browsing_labels_are_not_mistaken_for_purchases(self):
        # "Buy again" is a page you browse; "Don't buy now" is the opposite
        # of a purchase. Substring matching used to block both.
        for label in ("Buy again list", "Don't buy now", "paypal",
                      "Subscribe to newsletter", "Add to compare"):
            with self.subTest(label=label):
                self.assertFalse(is_submit_target(label))

    def test_ambiguous_words_alone_do_not_block(self):
        for label in ("Sort by price", "Reviews", "Next page"):
            with self.subTest(label=label):
                self.assertFalse(is_submit_target(label))

    def test_purchase_controls_are_recognised(self):
        for label in ("Buy now", "Add to cart", "Checkout", "Pay",
                      "Place order", "Kup teraz", "Zapłać"):
            with self.subTest(label=label):
                self.assertTrue(is_submit_target(label))

    def test_ordinary_controls_are_allowed(self):
        for label in ("Search", "Next page", "Show more", "Reviews",
                      "Sort by price", "", None):
            with self.subTest(label=label):
                self.assertFalse(is_submit_target(label))

    def test_the_guard_ignores_case(self):
        self.assertTrue(is_submit_target("BUY NOW"))
        self.assertTrue(is_submit_target("add TO cart"))


if __name__ == "__main__":
    unittest.main()
