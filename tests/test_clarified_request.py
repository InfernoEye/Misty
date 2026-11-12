import json
import unittest
import urllib.request

from app.brain.agent import BASE_URL, MistyAgent, parse_clarified_request
from tests.agent_fakes import FakeRegistry

PENDING = {
    "original_request": 'Open Cade and type in it "hello".',
    "clarification_question": "Could you please confirm the name or details of the application 'Cade'?",
}


def _server_up():
    try:
        with urllib.request.urlopen(f"{BASE_URL}/health", timeout=2) as response:
            return json.loads(response.read()).get("status") == "ok"
    except (OSError, ValueError):
        return False


@unittest.skipUnless(_server_up(), "shared local model server is not running on BASE_URL")
class ClarifiedRequestLiveTests(unittest.TestCase):
    def test_correction_replaces_the_misheard_target(self):
        rewritten = MistyAgent(registry=FakeRegistry())._clarified_request(
            PENDING, "I meant Kate."
        )
        self.assertIn("Kate", rewritten)
        self.assertNotIn("Cade", rewritten)


class ParseClarifiedRequestTests(unittest.TestCase):
    def test_a_valid_reply_yields_the_rewritten_request(self):
        self.assertEqual(
            parse_clarified_request('{"request": "Open Kate and type in it \\"hello\\"."}'),
            'Open Kate and type in it "hello".',
        )

    def test_unparseable_reply_is_rejected(self):
        self.assertIsNone(parse_clarified_request("not json"))

    def test_reply_without_a_request_field_is_rejected(self):
        self.assertIsNone(parse_clarified_request('{"text": "Open Kate"}'))

    def test_blank_or_missing_content_is_rejected(self):
        self.assertIsNone(parse_clarified_request('{"request": "   "}'))
        self.assertIsNone(parse_clarified_request(None))


if __name__ == "__main__":
    unittest.main()
