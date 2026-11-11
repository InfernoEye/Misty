"""Fail-closed rules for the deterministic direct-action shortcut.

``resolve_direct_action`` runs in front of the planner and in front of intent
confidence, so anything it claims is executed without a model in the loop.
These tests lock the two safety rules that keep it from acting on a request
it only partially understood:

* negation scoped to the action verb ("don't open Google", "nie otwieraj")
  must never be turned into the positive action;
* a sentence with a second independent instruction ("open X and list Y",
  "open X; then summarize") must never be shortened to just the first part.

Returning ``None`` hands the whole request to the planner, which can decline,
ask, or plan every part.
"""

import unittest

from app.brain.direct_actions import (
    _has_action_negation,
    resolve_conversational_reply,
    resolve_direct_action,
)


class NegationGuardTests(unittest.TestCase):
    def test_english_negated_commands_are_not_executed(self):
        for text in (
            "Don't open Google.",
            "Do not open Google",
            "dont open google.com",
            "don\u2019t open google",
            "DON'T OPEN GOOGLE!",
            "Please don't open Google.",
            "Never open YouTube.",
            "Never search YouTube for cats.",
            "Avoid opening reddit.com",
            "Stop trying to open Google",
            "Can you not open Google?",
            "I don't want you to open Google",
            "Don't summarize https://example.org",
        ):
            with self.subTest(text=text):
                self.assertIsNone(resolve_direct_action(text))

    def test_polish_negated_commands_are_not_executed(self):
        for text in (
            "Nie otwieraj Google",
            "nie otwieraj google.com",
            "Proszę nie otwieraj Google",
            "Nie uruchamiaj YouTube",
            "Nie szukaj na YouTube kotów",
            "nie wyszukuj w Google",
            "Nie idź do YouTube",
            "Nie wejdź na youtube.com",
            "Nigdy nie otwieraj YouTube",
        ):
            with self.subTest(text=text):
                self.assertIsNone(resolve_direct_action(text))

    def test_negation_helper_scopes_to_action_verbs(self):
        # A plain "nie" without an action verb is not a negated command.
        self.assertFalse(_has_action_negation("Otwórz YouTube"))
        self.assertFalse(_has_action_negation("Open Google"))
        self.assertTrue(_has_action_negation("Don't open Google"))
        self.assertTrue(_has_action_negation("nie otwieraj Google"))

    def test_negation_wins_over_a_following_positive_command(self):
        # Ambiguous follow-ups go to the planner rather than being guessed.
        for text in (
            "Don't open Google. Open Reddit.",
            "Never open Google or search for cats",
        ):
            with self.subTest(text=text):
                self.assertIsNone(resolve_direct_action(text))

    def test_unrelated_negation_does_not_block_a_clear_command(self):
        # "any" is not a negation, and "not" inside a search query is data.
        result = resolve_direct_action("Search YouTube for cats not dogs")
        self.assertEqual(result[0], "search_site")
        self.assertIn("cats not dogs", result[1]["query"])


class MultiIntentGuardTests(unittest.TestCase):
    def test_conjunction_with_second_domain_is_not_shortened(self):
        for text in (
            "Open Google and list all files in Downloads.",
            "Open Google and list the files in Downloads",
            "Open Google and then search for cats",
            "Open google.com and search for weather",
            "Open Reddit and tell me a joke",
            "Open example.com and summarize it",
            "Open YouTube and play lofi",
            "Find Minecraft videos and open Google",
            "Search Google for cats and open reddit",
            "Open example.com and move the report to Desktop",
            "Otwórz Google i wypisz pliki",
            "Otwórz YouTube potem pokaż pliki",
        ):
            with self.subTest(text=text):
                self.assertIsNone(resolve_direct_action(text))

    def test_semicolon_and_then_start_a_second_instruction(self):
        for text in (
            "Open Google; list all files in Downloads",
            "Open Google, then list files",
            "Open example.com; move the report to Desktop",
            "Search Google for cats; also open reddit",
            "Open Google then list files",
        ):
            with self.subTest(text=text):
                self.assertIsNone(resolve_direct_action(text))

    def test_compound_search_query_is_still_one_search(self):
        # "and" inside a search phrase is part of the query, not a second
        # instruction; this is one intent and must stay a direct search.
        for text, site, query in (
            ("Search YouTube for cats and dogs", "youtube", "cats and dogs"),
            (
                "Search Google for research and development",
                "google",
                "research and development",
            ),
            (
                "Search YouTube for python programming tutorials and exercises",
                "youtube",
                "python programming tutorials and exercises",
            ),
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    resolve_direct_action(text),
                    ("search_site", {"site": site, "query": query}),
                )

    def test_url_mentions_do_not_count_as_words_or_instructions(self):
        # "and"/"or"/"then" inside a URL path must not trip the guard, but a
        # real second instruction right after the URL must.
        self.assertEqual(
            resolve_direct_action(
                "Open https://en.wikipedia.org/wiki/And_or"
            ),
            ("open_url", {"url": "https://en.wikipedia.org/wiki/And_or"}),
        )
        self.assertIsNone(
            resolve_direct_action(
                "Open https://en.wikipedia.org/wiki/And_or and list my files"
            )
        )

    def test_single_command_still_resolves(self):
        # The guards must not make the plain navigation cases fall through.
        self.assertEqual(
            resolve_direct_action("Open Google."),
            ("open_url", {"url": "https://www.google.com/"}),
        )
        self.assertEqual(
            resolve_direct_action("Open example.com"),
            ("open_url", {"url": "https://example.com"}),
        )


class RobustnessTests(unittest.TestCase):
    def test_never_raises_and_returns_action_or_none(self):
        for text in (
            "",
            " ",
            "\n",
            None,
            5,
            [],
            "a" * 400,
            "open",
            "don't",
            "nie",
            ";",
            "then",
            "and",
            "open https://",
            "日本語",
            "Open Google\u2122",
            "open example.com/about?x=1&y=2#frag",
        ):
            with self.subTest(text=text):
                result = resolve_direct_action(text)
                if result is not None:
                    self.assertIsInstance(result, tuple)
                    self.assertEqual(len(result), 2)
                    self.assertIsInstance(result[0], str)
                    self.assertIsInstance(result[1], dict)

    def test_conversational_replies_unaffected(self):
        self.assertEqual(resolve_conversational_reply("Hello."), "Hello! How can I help?")
        self.assertIsNone(resolve_conversational_reply("Open Google"))


if __name__ == "__main__":
    unittest.main()
