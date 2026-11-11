import unittest

from app.brain.direct_actions import resolve_direct_action


class DirectActionTests(unittest.TestCase):
    def test_opens_known_site_from_clear_commands(self):
        for text in (
            "Open youtube.com",
            "Please open youtube.com.",
            "Proszę otwórz w przeglądarce YouTube'a.",
            "OpenYouTube.com",
            "Otwórz YouTube",
            "youtube.com",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    resolve_direct_action(text),
                    ("open_url", {"url": "https://www.youtube.com/"}),
                )

    def test_opens_arbitrary_public_domain(self):
        self.assertEqual(
            resolve_direct_action("Open example.com"),
            ("open_url", {"url": "https://example.com"}),
        )

    def test_site_search_builds_query(self):
        self.assertEqual(
            resolve_direct_action("Search YouTube for lo-fi hip-hop."),
            ("search_site", {"site": "youtube", "query": "lo-fi hip-hop"}),
        )
        self.assertEqual(
            resolve_direct_action("Szukaj na YouTube lo-fi hip hop"),
            ("search_site", {"site": "youtube", "query": "lo-fi hip hop"}),
        )
        self.assertEqual(
            resolve_direct_action("Find lofi on Wikipedia"),
            ("search_site", {"site": "wikipedia", "query": "lofi"}),
        )

    def test_leaves_reasoning_requests_to_the_model(self):
        for text in (
            "Research Arch Linux and summarize the top results",
            "Znajdź najnowsze informacje o prezydencie",
            "Open my Downloads folder",
            "Search the web for Arch Linux",
            "What time is it?",
        ):
            with self.subTest(text=text):
                self.assertIsNone(resolve_direct_action(text))

    def test_rejects_private_or_invalid_urls(self):
        self.assertIsNone(resolve_direct_action("Open http://127.0.0.1/"))
        self.assertIsNone(resolve_direct_action("Open localhost"))


if __name__ == "__main__":
    unittest.main()
