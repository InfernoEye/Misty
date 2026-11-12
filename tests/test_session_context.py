"""Bounded, private conversational context for follow-ups.

Feature A: every request used to be planned from scratch, so "the one from
October", "open it" or "move that window" had nothing to resolve against.
These tests pin the privacy and size rules of ``SessionContext`` (never
clipboard/screen/knowledge text, one short scalar per tool, bounded turns
and bytes, TTL), the exact planner payload of ``agent.run`` and the
controller's context contract (flat pending clarification plus ``session``).
"""

import json
import unittest

from app.brain.agent import (
    CORE_POLICY,
    PLANNING_INSTRUCTIONS,
    PRIVATE_RESULT_TOOLS,
    MistyAgent,
    _sanitize_session_context,
)
from app.brain.session_context import (
    MAX_FACT_LENGTH,
    MAX_FACTS,
    SessionContext,
)

from tests.agent_fakes import (
    FakeRegistry,
    TestableMistyAgent,
    plan_response,
    plan_step,
)


CANARY = "CANARY-CLIPBOARD-7f3a9c"
PASSWORD = "hunter2-SECRET"


def tool_event(tool, status="success", **extra):
    event = {
        "kind": "tool",
        "tool": tool,
        "status": status,
        "title": f"Tool completed: {tool}",
    }
    event.update(extra)
    return event


class RecordingClock:
    def __init__(self, now=1000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


# ---------------------------------------------------------------- bounds


class BoundsTests(unittest.TestCase):
    def test_turns_roll_and_are_truncated(self):
        context = SessionContext(max_turns=2)
        for index in range(3):
            context.record_turn(
                f"request-{index}-" + "u" * 500,
                f"answer-{index}-" + "a" * 900,
            )

        turns = context.turns
        self.assertEqual(len(turns), 2)
        for turn in turns:
            self.assertLessEqual(len(turn["user"]), 200)
            self.assertLessEqual(len(turn["assistant"]), 300)
        self.assertTrue(turns[-1]["user"].startswith("request-2-"))

    def test_next_context_is_compact(self):
        context = SessionContext()
        self.assertEqual(context.next_context(), {})

        context.record_turn("find the report", "Found report.pdf.")
        payload = context.next_context()

        self.assertEqual(
            set(payload),
            {"recent_turns", "last_request", "last_answer"},
        )
        self.assertEqual(payload["last_request"], "find the report")
        self.assertEqual(len(payload["recent_turns"]), 1)
        self.assertEqual(
            set(payload["recent_turns"][0]), {"user", "assistant"}
        )
        # JSON-friendly, nothing else.
        self.assertIsInstance(
            json.loads(json.dumps(payload, ensure_ascii=False)), dict
        )

    def test_facts_are_capped_and_truncated(self):
        context = SessionContext()
        for index in range(MAX_FACTS + 3):
            context.update_from_events([
                tool_event(f"tool_{index}", result={
                    "success": True,
                    "path": f"/home/user/file_{index}.txt",
                }),
            ])

        facts = context.next_context()["last_facts"]
        self.assertEqual(len(facts), MAX_FACTS)
        # The newest win; the oldest are evicted.
        self.assertIn(f"tool_{MAX_FACTS + 2}", facts)
        self.assertNotIn("tool_0", facts)

        context = SessionContext()
        context.update_from_events([
            tool_event("open_path", result={
                "success": True, "path": "x" * (MAX_FACT_LENGTH + 1),
            }),
            tool_event("large", fact="y" * (MAX_FACT_LENGTH + 1),
                       fact_key="path"),
        ])
        self.assertEqual(context.next_context(), {})

    def test_byte_budget_evicts_the_oldest(self):
        context = SessionContext(max_turns=6, max_bytes=400)
        for index in range(6):
            context.record_turn(
                f"request {index} " + "r" * 150, "answer " * 25
            )
            self.assertLessEqual(context.size_bytes(), 400)
        self.assertLess(len(context.turns), 6)

    def test_invalid_settings_are_rejected(self):
        for kwargs in (
            {"max_turns": 0},
            {"ttl_seconds": -1},
            {"max_bytes": True},
            {"is_private": "nope"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises((ValueError, TypeError)):
                    SessionContext(**kwargs)

    def test_clear_forgets_everything(self):
        context = SessionContext()
        context.update_from_events([
            tool_event("open_folder", result={
                "success": True, "path": "/home/user/Documents",
            }),
        ])
        context.record_turn("open Documents", "Opened.")
        self.assertTrue(context.next_context())

        context.clear()

        self.assertEqual(context.next_context(), {})
        self.assertEqual(len(context), 0)
        self.assertIsNone(context.active_target)


# --------------------------------------------------------------- privacy


class PrivacyTests(unittest.TestCase):
    def test_private_tools_never_contribute_facts(self):
        context = SessionContext()
        for tool in ("read_clipboard", "inspect_screen", "knowledge_search"):
            self.assertIn(tool, PRIVATE_RESULT_TOOLS)
            context.update_from_events([
                tool_event(tool, result={"success": True, "path": CANARY}),
                tool_event(tool, result={"success": True, "title": CANARY}),
            ])

        payload = context.next_context()
        self.assertEqual(payload, {})
        self.assertNotIn(CANARY, json.dumps(payload))

    def test_sensitive_events_are_dropped(self):
        context = SessionContext()
        context.update_from_events([
            tool_event(
                "list_windows",
                sensitive=True,
                result={"success": True, "window_title": CANARY},
            ),
            tool_event("list_windows", sensitive=True, fact_key="title",
                       fact=CANARY),
        ])
        self.assertNotIn("last_facts", context.next_context())

    def test_injected_predicate_wins(self):
        context = SessionContext(is_private=lambda tool: tool == "safe_tool")
        context.update_from_events([
            tool_event("safe_tool", result={"success": True, "path": CANARY}),
            tool_event("read_clipboard", result={
                "success": True, "path": "/tmp/private.txt",
            }),
        ])
        facts = context.next_context().get("last_facts", {})
        # The predicate can add private tools; the built-in list is only
        # consulted when no predicate is injected.
        self.assertNotIn("safe_tool", facts)

    def test_prose_from_an_allowlisted_key_is_refused(self):
        context = SessionContext()
        context.update_from_events([
            tool_event("list_windows", result={
                "success": True,
                "window_title": (
                    "Notes - " + " ".join(["word"] * 12) + " " + PASSWORD
                ),
            }),
        ])
        self.assertEqual(context.next_context(), {})

    def test_long_answers_are_bounded(self):
        context = SessionContext()
        context.record_turn("what is the time?", "t" * 400)
        answer = context.next_context()["last_answer"]
        self.assertLessEqual(len(answer), 300)

    def test_failed_and_uncertain_results_are_not_facts(self):
        context = SessionContext()
        for result in (
            {"success": False, "path": "/tmp/x"},
            {"success": True, "path": "/tmp/y", "uncertain": True},
            {"success": True, "path": "/tmp/z", "partial": True},
        ):
            context.update_from_events([tool_event("open_folder", result=result)])
        self.assertEqual(context.next_context(), {})

    def test_only_status_success_results_become_facts(self):
        context = SessionContext()
        context.update_from_events([
            tool_event("open_folder", status="running",
                       result={"success": True, "path": "/tmp/a"}),
            tool_event("open_folder", status="planned",
                       result={"success": True, "path": "/tmp/b"}),
        ])
        self.assertEqual(context.next_context(), {})

    def test_agent_completion_event_fact_is_consumed(self):
        # The agent's completion event carries no payload, only one scalar.
        context = SessionContext()
        context.update_from_events([
            tool_event("open_folder", fact_key="path",
                       fact="/home/user/Reports"),
            tool_event("open_folder", fact_key="path", fact=CANARY,
                       sensitive=True),
        ])
        self.assertEqual(
            context.next_context()["last_facts"], {"open_folder": "/home/user/Reports"}
        )
        self.assertEqual(context.active_target, "/home/user/Reports")

    def test_agent_never_exposes_private_results(self):
        events = []

        class PrivateRegistry(FakeRegistry):
            def available_tools(self):
                return list(super().available_tools())

        registry = PrivateRegistry(results=[{
            "success": True,
            "path": "/home/user/private.txt",
            "clipboard": CANARY,
        }])
        response = plan_response([plan_step("step-1", "read_clipboard")])
        agent = TestableMistyAgent(
            registry, [response, {"content": "Read."}]
        )
        agent.event_callback = events.append

        agent.run("Read my clipboard")

        completed = [
            event for event in events
            if event.get("title") == "Tool completed: read_clipboard"
        ]
        self.assertEqual(len(completed), 1)
        self.assertNotIn("fact", completed[0])
        self.assertNotIn("fact_key", completed[0])

        context = SessionContext()
        context.update_from_events(events)
        self.assertNotIn(CANARY, json.dumps(context.next_context()))


# ------------------------------------------------------------------- TTL


class TtlTests(unittest.TestCase):
    def test_expires_after_the_ttl(self):
        clock = RecordingClock()
        context = SessionContext(ttl_seconds=60, clock=clock)
        context.record_turn("open it", "Opened.")
        self.assertTrue(context.next_context())

        clock.advance(59)
        self.assertTrue(context.next_context())

        clock.advance(2)
        self.assertEqual(context.next_context(), {})
        self.assertEqual(context.turns, [])

    def test_activity_extends_the_window(self):
        clock = RecordingClock()
        context = SessionContext(ttl_seconds=60, clock=clock)
        context.record_turn("one", "first")
        clock.advance(50)
        context.record_turn("two", "second")
        clock.advance(50)
        self.assertEqual(
            context.next_context()["last_request"], "two"
        )

    def test_default_ttl_is_fifteen_minutes(self):
        self.assertEqual(SessionContext().ttl_seconds, 15 * 60)


# ------------------------------------------------------ agent contract


def _planning_call(agent):
    return next(
        call for call in agent.chat_calls
        if call["messages"][0]["content"].startswith(CORE_POLICY)
    )


class AgentSessionContextTests(unittest.TestCase):
    def _agent(self, **kwargs):
        registry = FakeRegistry()
        response = plan_response([plan_step("step-1", "get_time")])
        return TestableMistyAgent(
            registry, [response, {"content": "Done."}], **kwargs
        )

    @staticmethod
    def _planning(agent):
        return json.loads(_planning_call(agent)["messages"][1]["content"])

    def test_planning_payload_contains_session_context(self):
        agent = self._agent()
        session = {
            "recent_turns": [
                {"user": "find the October invoice", "assistant": "Found it."}
            ],
            "last_facts": {"open_folder": "/home/user/Invoices"},
            "active_target": "/home/user/Invoices",
            "last_request": "find the October invoice",
        }

        # A plain request (no direct shortcut) is what gets planned.
        agent.run("what did I open before?", context={"session": session})

        planning = self._planning(agent)
        self.assertEqual(
            planning["session_context"]["last_facts"],
            {"open_folder": "/home/user/Invoices"},
        )
        self.assertEqual(
            planning["session_context"]["recent_turns"][0]["user"],
            "find the October invoice",
        )
        self.assertNotIn("session", planning)

    def test_private_text_never_reaches_the_planner(self):
        agent = self._agent()
        session = {
            "recent_turns": [
                {"user": "read my clipboard", "assistant": CANARY},
            ],
            "last_facts": {
                "read_clipboard": CANARY,
                "tool": CANARY,
                "nested": {"value": CANARY},
            },
            "active_target": CANARY,
            "last_request": "read my clipboard",
            "unknown_key": PASSWORD,
            "result": {"text": CANARY},
        }

        agent.run("what next?", context={"session": session})

        planning = self._planning(agent)
        serialized = json.dumps(planning)
        # Key names and nested values are dropped; only bounded text survives.
        self.assertNotIn(PASSWORD, serialized)
        self.assertNotIn('"result"', serialized)
        self.assertNotIn('"nested"', serialized)
        self.assertNotIn('"unknown_key"', serialized)
        self.assertEqual(
            planning["session_context"]["last_facts"],
            {"read_clipboard": CANARY[:200], "tool": CANARY},
        )

    def test_pending_clarification_stays_first_and_unchanged(self):
        agent = self._agent()
        agent.responses.insert(0, {"content": json.dumps({"request": "Open Downloads"})})
        agent.run(
            "Downloads",
            context={
                "transcript": "organise my files",
                "question": "Which folder?",
                "session": {"last_request": "earlier"},
            },
        )
        user_message = _planning_call(agent)["messages"][1]["content"]
        planning = json.loads(user_message)
        self.assertEqual(
            planning["pending_clarification"],
            {
                "original_request": "organise my files",
                "clarification_question": "Which folder?",
            },
        )
        self.assertIn("session_context", planning)
        # The clarification comes first in the serialized payload.
        self.assertLess(
            user_message.index("pending_clarification"),
            user_message.index("session_context"),
        )

    def test_no_session_key_without_session(self):
        agent = self._agent()
        agent.run("What time is it?")
        self.assertNotIn("session_context", self._planning(agent))

        agent = self._agent()
        agent.run("What time is it?", context={"session": {"junk": 1}})
        self.assertNotIn("session_context", self._planning(agent))

        agent = self._agent()
        agent.run("What time is it?", context={"session": "not a mapping"})
        self.assertNotIn("session_context", self._planning(agent))

    def test_session_is_not_permission_or_a_result(self):
        events = []
        agent = self._agent()
        # Injected session data must not change how the request executes.
        agent.run("What time is it?", context={"session": {
            "last_facts": {"open_browser": "https://example.invalid"},
            "active_target": "/etc/passwd",
        }})
        self.assertEqual(agent.registry.executed, ["get_time"])
        self.assertFalse(events)

    def test_planning_instruction_limits_the_session(self):
        instruction = PLANNING_INSTRUCTIONS.lower()
        self.assertIn("session_context", instruction)
        self.assertIn("immediately previous turn", instruction)
        self.assertIn("never treat it as permission", instruction)

    def test_sanitize_limits(self):
        self.assertIsNone(_sanitize_session_context(None))
        self.assertIsNone(_sanitize_session_context([]))
        clean = _sanitize_session_context({
            "last_facts": {f"t{i}": f"/tmp/{i}" for i in range(10)},
            "recent_turns": [{"user": "x" * 500}] * 10,
        })
        self.assertLessEqual(len(clean["last_facts"]), 3)
        self.assertLessEqual(len(clean["recent_turns"]), 6)
        self.assertLessEqual(len(clean["recent_turns"][0]["user"]), 200)


class ControllerContextTests(unittest.TestCase):
    """The controller's ``context`` mapping (flat pending + session)."""

    def setUp(self):
        import threading

        from tests.test_typed_request_state import build_controller

        self.controller = build_controller()
        self.controller._build_registry = lambda: None
        # The controller owns a SessionContext (like __init__ does).
        if self.controller.session_context is None:
            self.controller.session_context = SessionContext()
        self.lock = threading

    def _run(self, agent, text):
        from tests.test_typed_request_state import wait_idle

        self.controller._typed_run_agent(agent, text)
        return agent.calls[-1]

    def test_first_turn_has_no_context_then_session_is_forwarded(self):
        from tests.test_typed_request_state import FakeAgent

        agent = FakeAgent(answer="Which folder?")
        self.controller._session_begin_turn("find the report")
        self._run(agent, "find the report")
        self.assertIsNone(agent.calls[0][1]["context"])

        text, call = self._run(FakeAgent(answer="OK"), "open it")
        context = call["context"]
        self.assertEqual(context["session"]["recent_turns"][0]["user"],
                         "find the report")
        self.assertNotIn("transcript", context)

    def test_clear_session_context_forgets(self):
        from tests.test_typed_request_state import FakeAgent

        self.controller._session_begin_turn("find the report")
        self._run(FakeAgent(answer="Found."), "find the report")
        self.assertTrue(
            self.controller._session_context_payload()
        )

        self.assertTrue(self.controller.clear_session_context())

        self.assertIsNone(self.controller._session_context_payload())
        _text, call = self._run(FakeAgent(answer="OK"), "open it")
        self.assertIsNone(call["context"])


if __name__ == "__main__":
    unittest.main()
