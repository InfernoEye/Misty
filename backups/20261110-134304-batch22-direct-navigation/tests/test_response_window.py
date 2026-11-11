import unittest

from app.ui.response_window import (
    AUTO_SHOW_MIN_CHARS,
    extract_source_urls,
    should_auto_open,
)


class ResponseWindowHelpersTests(unittest.TestCase):
    def test_extracts_unique_public_urls_and_strips_punctuation(self):
        text = (
            "See https://example.org/a, and https://example.org/a again. "
            "Also https://example.org/b?x=1."
        )

        urls = extract_source_urls(text)

        self.assertEqual(
            urls,
            ["https://example.org/a", "https://example.org/b?x=1"],
        )

    def test_rejects_private_and_invalid_urls(self):
        text = (
            "http://127.0.0.1/ https://localhost/ "
            "ftp://example.org/file https://example.org/ok"
        )

        self.assertEqual(
            extract_source_urls(text), ["https://example.org/ok"]
        )

    def test_auto_opens_long_or_sourced_answers(self):
        self.assertFalse(should_auto_open("Short answer."))
        self.assertTrue(
            should_auto_open("x" * AUTO_SHOW_MIN_CHARS)
        )
        self.assertTrue(
            should_auto_open("Read https://example.org/a")
        )


if __name__ == "__main__":
    unittest.main()
