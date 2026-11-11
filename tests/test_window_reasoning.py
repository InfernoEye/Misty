"""Generic semantic target binding: fake local model and catalog only."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from app.brain.window_reasoning import reason_window_target


def catalog():
    return {"success": True, "windows": [
        {"id": "win-files", "title": "Home — Dolphin", "app_id": "org.kde.dolphin",
         "resource_name": "dolphin", "description": "Manage folders and files", "pid": 123,
         "screen": "DP-1", "active": True, "geometry": {"x": -1200, "y": 0, "width": 1200, "height": 800}},
        {"id": "win-nebula", "title": "Project Nebula — StudioChat", "app_id": "example.studiochat",
         "description": "Team communication workspace", "active": False},
    ]}


def agent_with(reply):
    agent = Mock()
    agent._cancelled.return_value = False
    agent._chat.return_value = {"content": reply if isinstance(reply, str) else json.dumps(reply)}
    return agent


class WindowReasoningTests(unittest.TestCase):
    def test_generic_role_is_resolved_from_metadata_without_alias_table(self):
        agent = agent_with({"window_id": "win-files", "question": ""})
        result = reason_window_target(agent, "my file manager", catalog())
        self.assertTrue(result["success"])
        self.assertEqual(result["window_id"], "win-files")
        self.assertEqual(result["match"], "model_catalog")
        self.assertEqual(result["window"]["app_id"], "org.kde.dolphin")
        agent._chat.assert_called_once()
        args, kwargs = agent._chat.call_args
        self.assertEqual(kwargs, {"tools": [], "response_format": {"type": "json_object"}})
        request = json.loads(args[0][1]["content"])
        self.assertEqual(request["target"], "my file manager")
        self.assertNotIn("active", request["windows"][0])
        self.assertIn("untrusted data", args[0][0]["content"])
        self.assertIn("sole", args[0][0]["content"])

    def test_invented_application_uses_user_intent_and_actual_catalog(self):
        agent = agent_with({"window_id": "win-nebula", "question": ""})
        result = reason_window_target(agent, "the Nebula team communication workspace", catalog())
        self.assertEqual(result["window_id"], "win-nebula")
        self.assertIn("Nebula", result["window"]["title"])

    def test_transcription_reasoning_is_generic_and_catalog_bound(self):
        listing = {"success": True, "windows": [{"id": "opaque-7", "title": "AstralCord — chats",
                                                 "app_id": "example.astralcord"}]}
        agent = agent_with({"window_id": "opaque-7", "question": ""})
        self.assertTrue(reason_window_target(agent, "Astral court", listing)["success"])
        prompt = agent._chat.call_args.args[0][0]["content"]
        self.assertIn("speech/transcription errors", prompt)
        self.assertIn("Never choose merely", prompt)

    def test_hallucinated_id_is_rejected(self):
        result = reason_window_target(agent_with({"window_id": "invented", "question": ""}), "files", catalog())
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "window_reasoning_unknown_id")

    def test_ambiguity_null_returns_bounded_clarification(self):
        result = reason_window_target(agent_with({"window_id": None, "question": "Which workspace?"}), "workspace", catalog())
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "window_target_unresolved")
        self.assertEqual(result["question"], "Which workspace?")

    def test_null_empty_question_uses_safe_default(self):
        result = reason_window_target(agent_with({"window_id": None, "question": ""}), "workspace", catalog())
        self.assertEqual(result["question"], "Which open window did you mean?")

    def test_strict_response_rejects_bad_schema_nonfinite_and_duplicates(self):
        invalid = [
            "not json", "[]", "null", "1", '{}',
            '{"window_id":"win-files"}',
            '{"window_id":"win-files","question":"","extra":true}',
            '{"window_id":7,"question":""}',
            '{"window_id":true,"question":""}',
            '{"window_id":[],"question":""}',
            '{"window_id":"win-files","question":null}',
            '{"window_id":"win-files","question":NaN}',
            '{"window_id":Infinity,"question":""}',
            '{"window_id":-Infinity,"question":""}',
            '{"window_id":1e999,"question":""}',
            '{"window_id":"win-files","window_id":"win-nebula","question":""}',
            '{"window_id":"win-files","question":"","question":"other"}',
            json.dumps({"window_id": None, "question": "x" * 501}),
            json.dumps({"window_id": None, "question": "<b>Choose</b>"}),
            json.dumps({"window_id": None, "question": "unsafe\x00"}),
            json.dumps({"window_id": " " , "question": ""}),
            " " * 4001,
        ]
        for reply in invalid:
            with self.subTest(reply=reply[:80]):
                result = reason_window_target(agent_with(reply), "files", catalog())
                self.assertFalse(result["success"])
                self.assertEqual(result["code"], "window_reasoning_invalid_response")

    def test_tool_calls_and_nonmessage_reply_are_rejected(self):
        for message in ([], {"content": "{}", "tool_calls": [{"name": "move_window"}]}, {"content": 12}):
            with self.subTest(message=message):
                agent = agent_with({})
                agent._chat.return_value = message
                self.assertEqual(reason_window_target(agent, "files", catalog())["code"], "window_reasoning_invalid_response")

    def test_absent_model_answer_uses_ambiguity_safe_fallback(self):
        agent = agent_with({})
        agent._chat.return_value = None
        # Typo against live identity: resolved without inventing an ID.
        typo = reason_window_target(agent, "Dolfin", catalog())
        self.assertEqual((typo["success"], typo["window_id"], typo["match"]),
                         (True, "win-files", "fuzzy_identity"))
        # Unknown wording with no model: unavailable, never a blind choice.
        unknown = reason_window_target(agent, "my thing", catalog())
        self.assertFalse(unknown["success"])
        self.assertEqual(unknown["code"], "window_reasoning_unavailable")
        # Two equally close identities remain ambiguous.
        ambiguous = {"success": True, "windows": [
            {"id": "a", "app_id": "nebula"}, {"id": "b", "app_id": "nebulo"}]}
        self.assertFalse(reason_window_target(agent, "nebul", ambiguous)["success"])

    def test_empty_missing_or_failed_listing_never_calls_model(self):
        for listing in (None, {}, {"success": False, "windows": []},
                        {"success": True}, {"success": True, "windows": []},
                        {"success": 1, "windows": []}):
            with self.subTest(listing=listing):
                agent = agent_with({})
                result = reason_window_target(agent, "files", listing)
                self.assertFalse(result["success"])
                self.assertEqual(result["catalog"], [])
                agent._chat.assert_not_called()

    def test_malformed_catalog_metadata_never_calls_model(self):
        bad_windows = [None, {}, {"id": "x"}, {"id": ""}, {"id": " "}, {"id": 3}, {"id": "x" * 257},
                       {"id": "x", "title": 1}, {"id": "x", "title": "t" * 1001},
                       {"id": "x", "title": "bad\nline"}, {"id": "x", "title": "t", "pid": True},
                       {"id": "x", "title": "t", "minimized": 1},
                       {"id": "x", "title": "t", "geometry": {"x": 0, "y": 0, "width": 0, "height": 1}}]
        for window in bad_windows:
            with self.subTest(window=window):
                agent = agent_with({})
                result = reason_window_target(agent, "files", {"success": True, "windows": [window]})
                self.assertEqual(result["code"], "window_catalog_invalid")
                agent._chat.assert_not_called()

    def test_duplicate_ids_oversized_and_nonlist_catalog_never_call_model(self):
        windows = catalog()["windows"]
        for bad in ([windows[0], windows[0]], windows * 65, {"id": "x"}):
            with self.subTest(kind=type(bad).__name__):
                agent = agent_with({})
                self.assertEqual(reason_window_target(agent, "files", {"success": True, "windows": bad})["code"], "window_catalog_invalid")
                agent._chat.assert_not_called()

    def test_catalog_total_size_is_bounded_and_unknown_pid_zero_is_safe(self):
        listing = {"success": True, "windows": [{"id": str(i), "title": "t" * 1000,
                                                  "description": "d" * 1000} for i in range(128)]}
        agent = agent_with({})
        self.assertEqual(reason_window_target(agent, "files", listing)["code"], "window_catalog_invalid")
        agent._chat.assert_not_called()
        listing = {"success": True, "windows": [{"id": "unknown-pid", "title": "Workspace", "pid": 0}]}
        agent = agent_with({"window_id": "unknown-pid", "question": ""})
        self.assertTrue(reason_window_target(agent, "Workspace", listing)["success"])

    def test_invalid_target_never_calls_model(self):
        for target in (None, "", " ", "x" * 1001, "bad\x00text"):
            with self.subTest(target=target):
                agent = agent_with({})
                self.assertEqual(reason_window_target(agent, target, catalog())["code"], "window_target_invalid")
                agent._chat.assert_not_called()

    def test_cancel_before_chat_never_calls_model(self):
        agent = agent_with({"window_id": "win-files", "question": ""})
        agent._cancelled.return_value = True
        self.assertEqual(reason_window_target(agent, "files", catalog())["code"], "window_reasoning_canceled")
        agent._chat.assert_not_called()

    def test_cancel_after_chat_discards_valid_selection(self):
        agent = agent_with({"window_id": "win-files", "question": ""})
        agent._cancelled.side_effect = [False, True]
        result = reason_window_target(agent, "files", catalog())
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "window_reasoning_canceled")
        agent._chat.assert_called_once()

    def test_model_failure_has_no_retry_or_raw_error_echo(self):
        agent = agent_with({})
        agent._chat.side_effect = RuntimeError("PRIVATE MODEL ERROR")
        result = reason_window_target(agent, "files", catalog())
        self.assertEqual(result["code"], "window_reasoning_unavailable")
        self.assertNotIn("PRIVATE", str(result))
        agent._chat.assert_called_once()

    def test_sanitized_catalog_and_window_do_not_alias_input(self):
        listing = catalog()
        listing["windows"][0]["untrusted_extra"] = {"secret": "ignored"}
        before = deepcopy(listing)
        result = reason_window_target(agent_with({"window_id": "win-files", "question": ""}), "files", listing)
        result["window"]["geometry"]["x"] = 900
        self.assertEqual(listing, before)
        self.assertEqual(result["catalog"][0]["geometry"]["x"], -1200)
        self.assertNotIn("untrusted_extra", result["catalog"][0])


if __name__ == "__main__":
    unittest.main()
