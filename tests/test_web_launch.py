import unittest

from app.actions.web_launch import (
    build_site_search,
    is_blocked_hostname,
    is_public_ip,
    validate_open_url,
)


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


class NumericHostTests(unittest.TestCase):
    """Legacy/non-canonical numeric hosts must be refused, not reinterpreted."""

    def test_rejects_legacy_ipv4_spellings(self):
        for url in (
            "http://127.1:8080/",
            "http://127.1/",
            "http://127.0.1/",
            "http://2130706433:8080/",
            "http://2130706433/",
            "http://0x7f000001:8080/",
            "http://0x7f.0.0.1/",
            "http://0177.0.0.1/",
            "http://127.0.0.01/",
            "http://1.2.3.4./",
            "http://3232235777/",
            "http://192.168.257/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_open_url(url)

    def test_rejects_private_and_metadata_ip_literals(self):
        for url in (
            "http://169.254.169.254/latest/meta-data/",
            "http://0.0.0.0/",
            "http://100.64.0.1/",
            "http://224.0.0.1/",
            "http://255.255.255.255/",
            "http://[::]/",
            "http://[::ffff:127.0.0.1]/",
            "http://[fe80::1]/",
            "http://[fe80::1%25eth0]/",
            "http://[fc00::1]/",
            "http://[64:ff9b::127.0.0.1]/",
            "http://[2002:7f00:1::]/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_open_url(url)

    def test_accepts_canonical_public_ip_literals(self):
        self.assertEqual(validate_open_url("http://8.8.8.8/"), "http://8.8.8.8/")
        self.assertEqual(
            validate_open_url("https://8.8.8.8:443/x?y=1"),
            "https://8.8.8.8:443/x?y=1",
        )
        self.assertEqual(
            validate_open_url("http://[2606:4700:4700::1111]/"),
            "http://[2606:4700:4700::1111]/",
        )

    def test_rejects_userinfo_aliases_and_backslashes(self):
        for url in (
            "http://user@8.8.8.8/",
            "http://user:pw@example.org/",
            "http://example.org\\@127.0.0.1/",
            "http://example.com\\evil.example/",
            "http://exam\ttple.com/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_open_url(url)

    def test_rejects_local_names(self):
        for url in (
            "http://foo/",
            "http://LOCALHOST/",
            "http://a.localhost/",
            "http://printer.local/",
            "http://db.internal/",
            "http://router.home.arpa/",
            "http://example.123/",
            "http://bad_host.example/",
            "http://-example.org/",
            "http://example..org/",
            "http://\u00fcber.example/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_open_url(url)

    def test_normalizes_scheme_host_and_fragment(self):
        self.assertEqual(
            validate_open_url("HTTPS://Example.ORG/Path?Q=1#frag"),
            "https://example.org/Path?Q=1",
        )
        self.assertEqual(
            validate_open_url("http://example.org?x=1"),
            "http://example.org/?x=1",
        )


class PublicIpHelperTests(unittest.TestCase):
    def test_public_addresses(self):
        for value in (
            "8.8.8.8",
            "93.184.216.34",
            "[2606:4700:4700::1111]",
            "2606:4700:4700::1111",
        ):
            with self.subTest(value=value):
                self.assertTrue(is_public_ip(value))

    def test_non_public_or_non_ip_values(self):
        for value in (
            "127.0.0.1",
            "10.0.0.1",
            "169.254.169.254",
            "0.0.0.0",
            "::1",
            "::ffff:10.0.0.1",
            "fe80::1%eth0",
            "example.com",
            "127.1",
            "",
            None,
            123,
        ):
            with self.subTest(value=value):
                self.assertFalse(is_public_ip(value))

    def test_blocked_hostname_helper_never_raises(self):
        for value in ("localhost", "127.0.0.1", "example.com", "127.1", None):
            with self.subTest(value=value):
                self.assertIsInstance(is_blocked_hostname(value), bool)
        self.assertFalse(is_blocked_hostname("example.com"))
        self.assertTrue(is_blocked_hostname("127.0.0.1"))
        self.assertTrue(is_blocked_hostname(None))


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
