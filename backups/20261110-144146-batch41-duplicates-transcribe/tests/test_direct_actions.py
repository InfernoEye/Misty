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
        self.assertEqual(
            resolve_direct_action("Find me Katniss Everdeen on YouTube."),
            (
                "search_site",
                {"site": "youtube", "query": "Katniss Everdeen"},
            ),
        )
        for text in (
            "Find me on YouTube a cool Minecraft video.",
            "Find me on YouTube, cool Minecraft video.",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    resolve_direct_action(text),
                    (
                        "search_site",
                        {"site": "youtube", "query": "cool Minecraft video"},
                    ),
                )
        self.assertEqual(
            resolve_direct_action("Search on YouTube for Katniss Everdeen."),
            (
                "search_site",
                {"site": "youtube", "query": "Katniss Everdeen"},
            ),
        )
        for text in (
            "What are some cool Minecraft videos on YouTube?",
            "Show me cool Minecraft videos on YouTube",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    resolve_direct_action(text),
                    (
                        "search_site",
                        {"site": "youtube", "query": "cool Minecraft videos"},
                    ),
                )

    def test_open_site_with_query_uses_search(self):
        for text, site, query in (
            ("Open Google with Katniss Everdeen.", "google", "Katniss Everdeen"),
            ("Open Reddit for Misty.", "reddit", "Misty"),
            ("Open YouTube with lofi hip hop.", "youtube", "lofi hip hop"),
            ("Google Katniss Everdeen.", "google", "Katniss Everdeen"),
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    resolve_direct_action(text),
                    ("search_site", {"site": site, "query": query}),
                )
        self.assertIsNone(resolve_direct_action("Google is a search engine."))

    def test_generic_domain_search(self):
        self.assertEqual(
            resolve_direct_action("Search on example.com for Misty"),
            (
                "search_site",
                {"site": "google", "query": "site:example.com Misty"},
            ),
        )

    def test_watch_intent_becomes_youtube_choices(self):
        for text, query in (
            ("Find me something cool to watch.", "cool videos to watch"),
            ("What's trending on YouTube?", "trending videos"),
            ("What is popular on YouTube?", "popular videos"),
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    resolve_direct_action(text),
                    ("search_site", {"site": "youtube", "query": query}),
                )

    def test_page_and_screen_summary_requests(self):
        page = resolve_direct_action("Can you summarize the page I'm on?")
        self.assertEqual(page[0], "inspect_screen")
        self.assertEqual(page[1]["mode"], "active_window")
        self.assertIn("Summarize", page[1]["prompt"])

        screen = resolve_direct_action(
            "Summarize what I'm seeing on the left screen."
        )
        self.assertEqual(screen[0], "inspect_screen")
        self.assertEqual(screen[1]["mode"], "screen")
        self.assertEqual(screen[1]["screen_name"], "left")

        right = resolve_direct_action("What is on the right monitor?")
        self.assertEqual(right[1]["screen_name"], "right")

        generic = resolve_direct_action("Summarize what I'm seeing.")
        self.assertEqual(generic[0], "inspect_screen")
        self.assertEqual(generic[1]["screen_name"], "primary")

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
