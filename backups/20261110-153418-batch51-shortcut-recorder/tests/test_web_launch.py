import unittest

from app.actions.web_launch import build_site_search, validate_open_url


class ValidateOpenUrlTests(unittest.TestCase):
    def test_accepts_public_https_and_http(self):
        for url in (
            "https://www.youtube.com/",
            "http://example.org/page?q=1",
        ):
            with self.subTest(url=url):
                self.assertEqual(validate_open_url(url), url)

    def test_rejects_non_web_and_credentials(self):
        for url in (
            "file:///etc/passwd",
            "javascript:alert(1)",
            "ftp://example.org/file",
            "https://user:pass@example.org/",
            "https://example.org/\x00",
            "x" * 2049,
            "",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_open_url(url)

    def test_rejects_local_and_private_literals(self):
        for url in (
            "http://localhost/",
            "http://127.0.0.1/",
            "http://10.0.0.1/",
            "http://192.168.1.1/",
            "http://[::1]/",
            "http://router.local/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_open_url(url)


class SiteSearchTests(unittest.TestCase):
    def test_youtube_search_encodes_query(self):
        url = build_site_search("YouTube", "lofi hip hop")
        self.assertEqual(
            url,
            "https://www.youtube.com/results?search_query=lofi+hip+hop",
        )

    def test_supported_sites_build_public_urls(self):
        for site in ("google", "wikipedia", "github", "archwiki"):
            with self.subTest(site=site):
                url = build_site_search(site, "misty assistant")
                self.assertTrue(url.startswith("https://"))
                self.assertIn("misty+assistant", url)

    def test_rejects_unknown_site_and_bad_query(self):
        for site, query in (
            ("unknown", "x"),
            ("youtube", ""),
            ("youtube", "x" * 201),
            ("youtube", "bad\x00query"),
            (123, "x"),
        ):
            with self.subTest(site=site, query=query):
                with self.assertRaises(ValueError):
                    build_site_search(site, query)


if __name__ == "__main__":
    unittest.main()
