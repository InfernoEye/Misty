import unittest

from app.ui.response_window import (
    AUTO_SHOW_MIN_CHARS,
    extract_source_urls,
    format_response_html,
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

    def test_file_list_is_rendered_as_bullets(self):
        text = (
            "The files in your Downloads folder are: "
            "one.png, two.jpg, three.mp3, four.wav, five.zip."
        )

        rendered = format_response_html(text)

        self.assertIn("<p>The files in your Downloads folder are:</p>", rendered)
        self.assertEqual(rendered.count('class="misty-item"'), 5)
        self.assertIn("<code>one.png</code>", rendered)
        self.assertIn("<code>five.zip</code>", rendered)

    def test_generic_comma_prose_is_not_split(self):
        rendered = format_response_html(
            "Summary: alpha, beta, gamma, delta, epsilon."
        )

        self.assertEqual(rendered.count('class="misty-item"'), 0)
        self.assertIn("alpha, beta, gamma", rendered)

    def test_paragraphs_bullets_and_headings_are_formatted(self):
        text = "## Summary\nFirst paragraph.\n\n- one\n- two\n\nLast."

        rendered = format_response_html(text)

        self.assertIn("<h2>Summary</h2>", rendered)
        self.assertIn("<p>First paragraph.</p>", rendered)
        self.assertEqual(rendered.count('class="misty-item"'), 2)
        self.assertIn("<p>Last.</p>", rendered)

    def test_html_is_escaped(self):
        rendered = format_response_html("<script>alert(1)</script>")

        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_auto_opens_long_or_sourced_answers(self):
        self.assertFalse(should_auto_open("Short answer."))
        self.assertTrue(
            should_auto_open("x" * AUTO_SHOW_MIN_CHARS)
        )
        self.assertTrue(
            should_auto_open("Read https://example.org/a")
        )
        self.assertTrue(
            should_auto_open("Opening https://www.youtube.com/ in your browser.")
        )
        self.assertTrue(
            should_auto_open("Searching youtube for 'x' in your browser.")
        )


if __name__ == "__main__":
    unittest.main()
