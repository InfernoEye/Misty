"""Generic research relevance and readability: no topic or site special cases."""
import unittest

from app.brain.tools import _readable_excerpt, _relevant_research_result


class ResearchRelevanceTests(unittest.TestCase):
    def test_capitalized_topic_word_matches_lowercase_title(self):
        item = {"title": "kernel bug patched", "snippet": "A kernel vulnerability was fixed.",
                "url": "https://example.org/a"}
        self.assertTrue(_relevant_research_result("newest Kernel vulnerability", item))

    def test_short_place_name_needs_a_whole_word(self):
        unrelated = {"title": "Thus we proceed with elections", "url": "https://e.org/x"}
        related = {"title": "US elections head to a runoff", "url": "https://e.org/y"}
        self.assertFalse(_relevant_research_result("election in the US", unrelated))
        self.assertTrue(_relevant_research_result("election in the US", related))

    def test_request_framing_words_do_not_count_as_the_topic(self):
        item = {"title": "Dolphin file manager release", "url": "https://e.org/z"}
        self.assertTrue(_relevant_research_result("newest info on Dolphin", item))

    def test_off_topic_results_still_rejected(self):
        item = {"title": "Windows update roundup", "url": "https://e.org/w"}
        self.assertFalse(_relevant_research_result("newest Linux vulnerability", item))


class ReadableExcerptTests(unittest.TestCase):
    def test_navigation_and_ads_are_dropped_but_article_sentences_kept(self):
        text = "\n".join([
            "Featured Funds | Nifty 50 25,044.65 (+0.25%) | Sensex 82,000.12",
            "Markets rallied after the announcement of a ceasefire agreement today.",
            "Subscribe to our newsletter for more stories about world affairs.",
            "Officials said negotiations would resume next week in Geneva.",
        ])
        excerpt = _readable_excerpt(text, 900)
        self.assertIn("ceasefire", excerpt)
        self.assertIn("Geneva", excerpt)
        self.assertNotIn("Nifty", excerpt)
        self.assertNotIn("newsletter", excerpt)

    def test_falls_back_to_condensed_text_and_always_bounds_length(self):
        self.assertEqual(_readable_excerpt("short", 100), "short")
        self.assertLessEqual(len(_readable_excerpt("word " * 5000, 400)), 400)


if __name__ == "__main__":
    unittest.main()
