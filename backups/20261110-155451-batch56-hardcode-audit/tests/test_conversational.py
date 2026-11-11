import unittest

from app.brain.agent import MistyAgent
from app.brain.direct_actions import resolve_conversational_reply


class ConversationalReplyTests(unittest.TestCase):
    def test_greetings_and_thanks_are_answered_locally(self):
        for text, expected in (
            ("Hello.", "Hello! How can I help?"),
            ("hi", "Hello! How can I help?"),
            ("Thank you.", "You're welcome!"),
            ("Dziękuję!", "You're welcome!"),
            ("How are you?", "I'm running locally and ready. What can I do?"),
        ):
            with self.subTest(text=text):
                self.assertEqual(resolve_conversational_reply(text), expected)

    def test_requests_are_not_mistaken_for_greetings(self):
        for text in (
            "Hello, open Google",
            "Thanks, now list Downloads",
            "open browser",
        ):
            with self.subTest(text=text):
                self.assertIsNone(resolve_conversational_reply(text))

    def test_agent_short_circuits_without_tools(self):
        agent = MistyAgent(registry=object())
        self.assertEqual(agent.run("Hello."), "Hello! How can I help?")
        self.assertEqual(agent.run("Thank you."), "You're welcome!")


if __name__ == "__main__":
    unittest.main()
