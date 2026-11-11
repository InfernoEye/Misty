"""Tests for the isolated, SSRF-resistant web fetch module.

Every test mocks DNS and the HTTP transport.  No test opens a socket, uses a
browser, or contacts a real server.
"""

import codecs
import email.message
import importlib
import io
import json
import socket
import unittest
import urllib.error
from unittest import mock

fetch_module = importlib.import_module("app.web.fetch")
fetch = fetch_module.fetch

PUBLIC_IP = "93.184.216.34"
PUBLIC_IP_2 = "1.1.1.1"


def _addr(ip, port=80):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    if family == socket.AF_INET6:
        sockaddr = (ip, port, 0, 0)
    else:
        sockaddr = (ip, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


def _http_error(code, location=None, url="http://example.com/"):
    headers = email.message.Message()
    if location is not None:
        headers["Location"] = location
    return urllib.error.HTTPError(
        url, code, f"HTTP {code}", headers, io.BytesIO(b"")
    )


class FakeResponse:
    """Minimal stand-in for an http.client response."""

    def __init__(
        self,
        body=b"",
        status=200,
        content_type="text/html; charset=utf-8",
        headers=None,
        chunk_size=None,
    ):
        self._body = body
        self._offset = 0
        self.status = status
        self.headers = email.message.Message()
        if content_type is not None:
            self.headers["Content-Type"] = content_type
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self.chunk_size = chunk_size
        self.closed = False
        self.read_calls = 0

    def read(self, n=-1):
        self.read_calls += 1
        remaining = len(self._body) - self._offset
        if n is None or n < 0:
            size = remaining
        else:
            size = min(n, remaining)
        if self.chunk_size is not None:
            size = min(size, self.chunk_size)
        chunk = self._body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def getcode(self):
        return self.status

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


class ReadTimeoutResponse(FakeResponse):
    def read(self, n=-1):
        raise TimeoutError("read timed out")


class WebFetchTestCase(unittest.TestCase):
    def setUp(self):
        dns = mock.patch.object(
            fetch_module.socket,
            "getaddrinfo",
            return_value=[_addr(PUBLIC_IP)],
        )
        self.getaddrinfo = dns.start()
        self.addCleanup(dns.stop)

        self.default_response = FakeResponse(
            body=b"<html><head><title>Default</title></head>"
            b"<body><p>ok</p></body></html>"
        )
        opener = mock.patch.object(
            fetch_module, "_open_url", return_value=self.default_response
        )
        self.open_mock = opener.start()
        self.addCleanup(opener.stop)

    def use_open(self, *items):
        patcher = mock.patch.object(
            fetch_module, "_open_url", side_effect=list(items)
        )
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def use_dns(self, *answers):
        patcher = mock.patch.object(
            fetch_module.socket, "getaddrinfo", side_effect=list(answers)
        )
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked


class URLValidationTests(WebFetchTestCase):
    def test_rejects_non_http_schemes(self):
        for url in (
            "file:///etc/passwd",
            "ftp://example.com/file",
            "javascript:alert(1)",
            "data:text/plain,hello",
        ):
            with self.subTest(url=url):
                with self.assertRaises(fetch_module.InvalidURLError):
                    fetch(url)
        self.open_mock.assert_not_called()

    def test_rejects_embedded_credentials(self):
        for url in (
            "http://user:pass@example.com/",
            "https://user@example.com/",
            "http://:pass@example.com/",
            "http://user%40name:pass@example.com/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(fetch_module.InvalidURLError):
                    fetch(url)
        self.open_mock.assert_not_called()

    def test_rejects_odd_ports(self):
        for url in (
            "http://example.com:8080/",
            "https://example.com:8443/",
            "http://example.com:22/",
            "http://example.com:0/",
            "http://example.com:65536/",
            "http://example.com:abc/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(fetch_module.InvalidURLError):
                    fetch(url)
        self.open_mock.assert_not_called()

    def test_allows_default_ports(self):
        result = fetch("http://example.com:80/")
        self.assertEqual(result.status, 200)
        result = fetch("https://example.com:443/")
        self.assertEqual(result.status, 200)

    def test_rejects_malformed_urls(self):
        for url in (
            "",
            "   ",
            "not a url",
            "http://",
            "http://exa mple.com/",
            "http://[::1",
            "http://example.com/\x00",
            "http://example.com/a\nb",
        ):
            with self.subTest(url=url):
                with self.assertRaises(fetch_module.InvalidURLError):
                    fetch(url)
        self.open_mock.assert_not_called()

    def test_rejects_non_string_and_overlong_urls(self):
        for value in (None, 123, b"http://example.com/"):
            with self.subTest(value=value):
                with self.assertRaises(fetch_module.InvalidURLError):
                    fetch(value)
        with self.assertRaises(fetch_module.InvalidURLError):
            fetch("http://example.com/" + "a" * 9000)
        self.open_mock.assert_not_called()

    def test_strips_fragment(self):
        result = fetch("http://example.com/dir/page#section")
        self.assertEqual(result.final_url, "http://example.com/dir/page")
        self.assertEqual(
            fetch_module.validate_url("http://example.com/dir/page#x"),
            "http://example.com/dir/page",
        )

    def test_validate_url_rejects_public_host_with_odd_port(self):
        with self.assertRaises(fetch_module.InvalidURLError):
            fetch_module.validate_url("https://example.com:9443/")


class AddressValidationTests(WebFetchTestCase):
    def test_rejects_private_ip_literals(self):
        addresses = (
            "127.0.0.1",
            "127.1.2.3",
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.1.1",
            "224.0.0.1",
            "239.255.255.250",
            "0.0.0.0",
            "100.64.0.1",
            "198.18.0.1",
            "::1",
            "fe80::1",
            "fc00::1",
            "ff02::1",
            "::",
            "::ffff:127.0.0.1",
        )
        for address in addresses:
            host = f"[{address}]" if ":" in address else address
            with self.subTest(address=address):
                with self.assertRaises(fetch_module.BlockedAddressError):
                    fetch(f"http://{host}/")
        self.getaddrinfo.assert_not_called()
        self.open_mock.assert_not_called()

    def test_rejects_localhost_resolving_to_loopback(self):
        self.use_dns([_addr("127.0.0.1")])
        with self.assertRaises(fetch_module.BlockedAddressError):
            fetch("http://localhost/")
        self.open_mock.assert_not_called()

    def test_rejects_hostname_resolving_to_private_address(self):
        self.use_dns([_addr("192.168.0.10")])
        with self.assertRaises(fetch_module.BlockedAddressError):
            fetch("http://internal.example/")
        self.open_mock.assert_not_called()

    def test_rejects_if_any_dns_answer_is_private(self):
        self.use_dns([_addr(PUBLIC_IP), _addr("10.1.2.3")])
        with self.assertRaises(fetch_module.BlockedAddressError):
            fetch("http://mixed.example/")
        self.open_mock.assert_not_called()

    def test_rejects_ipv6_loopback_dns_answer(self):
        self.use_dns([_addr("::1")])
        with self.assertRaises(fetch_module.BlockedAddressError):
            fetch("http://v6.example/")
        self.open_mock.assert_not_called()

    def test_allows_public_hostname_with_mocked_dns(self):
        result = fetch("http://example.com/")
        self.assertEqual(result.status, 200)
        self.assertEqual(result.final_url, "http://example.com/")
        self.getaddrinfo.assert_called_once_with(
            "example.com", 80, type=socket.SOCK_STREAM
        )

    def test_allows_public_https_hostname_with_port_443(self):
        result = fetch("https://example.com/")
        self.assertEqual(result.status, 200)
        self.getaddrinfo.assert_called_once_with(
            "example.com", 443, type=socket.SOCK_STREAM
        )

    def test_allows_public_ip_literal_without_dns(self):
        result = fetch(f"http://{PUBLIC_IP}/")
        self.assertEqual(result.status, 200)
        self.getaddrinfo.assert_not_called()

    def test_dns_failure_is_network_error(self):
        self.use_dns(socket.gaierror(-2, "Name or service not known"))
        with self.assertRaises(fetch_module.NetworkError):
            fetch("http://missing.example/")
        self.open_mock.assert_not_called()

    def test_empty_dns_answer_is_network_error(self):
        self.use_dns([])
        with self.assertRaises(fetch_module.NetworkError):
            fetch("http://empty.example/")
        self.open_mock.assert_not_called()

    def test_unparseable_dns_answer_is_blocked(self):
        self.use_dns([(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("?", 80))])
        with self.assertRaises(fetch_module.BlockedAddressError):
            fetch("http://weird.example/")
        self.open_mock.assert_not_called()


class RedirectTests(WebFetchTestCase):
    def test_follows_valid_redirect_and_updates_final_url(self):
        opener = self.use_open(
            _http_error(302, "/next"),
            FakeResponse(
                body=b"<html><head><title>Next</title></head>"
                b"<body><p>landed</p></body></html>"
            ),
        )
        result = fetch("http://example.com/start")
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(result.final_url, "http://example.com/next")
        self.assertEqual(result.status, 200)
        self.assertEqual(result.title, "Next")
        self.assertIn("landed", result.text)

    def test_redirect_to_private_literal_is_rejected(self):
        opener = self.use_open(_http_error(302, "http://127.0.0.1/secret"))
        with self.assertRaises(fetch_module.BlockedAddressError):
            fetch("http://example.com/start")
        self.assertEqual(opener.call_count, 1)

    def test_redirect_to_hostname_resolving_private_is_rejected(self):
        self.use_dns([_addr(PUBLIC_IP)], [_addr("10.1.2.3")])
        opener = self.use_open(
            _http_error(302, "http://internal.example/secret")
        )
        with self.assertRaises(fetch_module.BlockedAddressError):
            fetch("http://example.com/start")
        self.assertEqual(opener.call_count, 1)

    def test_redirect_chain_is_bounded(self):
        opener = self.use_open(
            *[_http_error(302, "/next") for _ in range(10)]
        )
        with self.assertRaises(fetch_module.TooManyRedirectsError):
            fetch("http://example.com/start", max_redirects=5)
        self.assertEqual(opener.call_count, 6)

    def test_redirect_limit_zero(self):
        opener = self.use_open(_http_error(302, "/next"))
        with self.assertRaises(fetch_module.TooManyRedirectsError):
            fetch("http://example.com/start", max_redirects=0)
        self.assertEqual(opener.call_count, 1)

    def test_redirect_without_location_is_http_error(self):
        self.use_open(_http_error(302, None))
        with self.assertRaises(fetch_module.HTTPStatusError) as raised:
            fetch("http://example.com/start")
        self.assertEqual(raised.exception.status, 302)

    def test_redirect_fragment_is_stripped(self):
        self.use_open(
            _http_error(302, "/next#frag"),
            FakeResponse(body=b"<p>ok</p>"),
        )
        result = fetch("http://example.com/start")
        self.assertEqual(result.final_url, "http://example.com/next")

    def test_redirect_to_odd_port_is_rejected(self):
        self.use_open(_http_error(302, "http://example.com:8080/x"))
        with self.assertRaises(fetch_module.InvalidURLError):
            fetch("http://example.com/start")

    def test_redirect_to_credentials_is_rejected(self):
        self.use_open(
            _http_error(302, "http://user:pass@example.com/x")
        )
        with self.assertRaises(fetch_module.InvalidURLError):
            fetch("http://example.com/start")

    def test_opener_refuses_automatic_redirects(self):
        handler = fetch_module._NoRedirectHandler()
        self.assertIsNone(
            handler.redirect_request(
                None, None, 302, "Found", {}, "http://example.com/next"
            )
        )


class ResponsePolicyTests(WebFetchTestCase):
    def test_rejects_disallowed_content_type(self):
        response = FakeResponse(
            body=b'{"ok": true}', content_type="application/json"
        )
        self.use_open(response)
        with self.assertRaises(
            fetch_module.UnsupportedContentTypeError
        ) as raised:
            fetch("http://example.com/data")
        self.assertEqual(raised.exception.content_type, "application/json")
        self.assertTrue(response.closed)

    def test_rejects_missing_content_type(self):
        response = FakeResponse(body=b"hello", content_type=None)
        self.use_open(response)
        with self.assertRaises(fetch_module.UnsupportedContentTypeError):
            fetch("http://example.com/data")

    def test_accepts_content_type_with_parameters_and_case(self):
        self.use_open(
            FakeResponse(
                body=b"<p>ok</p>",
                content_type="TEXT/HTML; Charset=UTF-8",
            )
        )
        result = fetch("http://example.com/")
        self.assertEqual(result.content_type, "text/html")
        self.assertIn("ok", result.text)

    def test_oversized_body_is_aborted(self):
        response = FakeResponse(
            body=b"x" * 1000, content_type="text/plain", chunk_size=64
        )
        self.use_open(response)
        with self.assertRaises(fetch_module.ResponseTooLargeError):
            fetch("http://example.com/big", max_bytes=100)
        self.assertTrue(response.closed)

    def test_declared_content_length_aborts_before_read(self):
        response = FakeResponse(
            body=b"x" * 10,
            content_type="text/plain",
            headers={"Content-Length": "9999"},
        )
        self.use_open(response)
        with self.assertRaises(fetch_module.ResponseTooLargeError):
            fetch("http://example.com/big", max_bytes=100)
        self.assertEqual(response.read_calls, 0)
        self.assertTrue(response.closed)

    def test_body_exactly_at_limit_is_allowed(self):
        self.use_open(
            FakeResponse(body=b"a" * 100, content_type="text/plain")
        )
        result = fetch("http://example.com/", max_bytes=100)
        self.assertEqual(result.bytes_read, 100)
        self.assertEqual(len(result.text), 100)

    def test_connect_timeout_is_reported(self):
        self.use_open(TimeoutError("connect timed out"))
        with self.assertRaises(fetch_module.FetchTimeoutError):
            fetch("http://example.com/")

    def test_read_timeout_is_reported(self):
        self.use_open(ReadTimeoutResponse(content_type="text/plain"))
        with self.assertRaises(fetch_module.FetchTimeoutError):
            fetch("http://example.com/")

    def test_http_error_status_is_reported(self):
        self.use_open(_http_error(404))
        with self.assertRaises(fetch_module.HTTPStatusError) as raised:
            fetch("http://example.com/missing")
        self.assertEqual(raised.exception.status, 404)

    def test_http_500_is_reported(self):
        self.use_open(_http_error(500))
        with self.assertRaises(fetch_module.HTTPStatusError) as raised:
            fetch("http://example.com/boom")
        self.assertEqual(raised.exception.status, 500)

    def test_network_error_is_reported(self):
        self.use_open(urllib.error.URLError(ConnectionRefusedError("no")))
        with self.assertRaises(fetch_module.NetworkError):
            fetch("http://example.com/")

    def test_latin1_charset_is_decoded(self):
        body = "<html><head><title>café</title></head><body><p>café</p></body></html>".encode(
            "iso-8859-1"
        )
        self.use_open(
            FakeResponse(
                body=body, content_type="text/html; charset=iso-8859-1"
            )
        )
        result = fetch("http://example.com/")
        self.assertEqual(result.charset, "iso-8859-1")
        self.assertEqual(result.title, "café")
        self.assertIn("café", result.text)

    def test_unknown_charset_falls_back_to_utf8(self):
        self.use_open(
            FakeResponse(
                body="<p>ok</p>".encode("utf-8"),
                content_type="text/html; charset=not-a-real-charset",
            )
        )
        result = fetch("http://example.com/")
        self.assertEqual(result.charset, "utf-8")
        self.assertIn("ok", result.text)

    def test_meta_charset_is_sniffed_when_header_omits_it(self):
        body = (
            '<html><head><meta charset="iso-8859-1"><title>café</title>'
            "</head><body><p>café</p></body></html>"
        ).encode("iso-8859-1")
        self.use_open(FakeResponse(body=body, content_type="text/html"))
        result = fetch("http://example.com/")
        self.assertEqual(result.charset, "iso-8859-1")
        self.assertEqual(result.title, "café")

    def test_utf8_bom_is_handled(self):
        self.use_open(
            FakeResponse(
                body=codecs.BOM_UTF8 + "<p>hé</p>".encode("utf-8"),
                content_type="text/html",
            )
        )
        result = fetch("http://example.com/")
        self.assertEqual(result.charset, "utf-8-sig")
        self.assertIn("hé", result.text)

    def test_plain_text_is_returned_without_html_parsing(self):
        self.use_open(
            FakeResponse(
                body=b"Hello\nworld\n\n", content_type="text/plain"
            )
        )
        result = fetch("http://example.com/notes.txt")
        self.assertEqual(result.title, "")
        self.assertEqual(result.links, [])
        self.assertEqual(result.text, "Hello\nworld")
        self.assertEqual(result.content_type, "text/plain")

    def test_plain_text_respects_character_limit(self):
        self.use_open(
            FakeResponse(body=b"abcdefghij", content_type="text/plain")
        )
        result = fetch("http://example.com/notes.txt", max_chars=5)
        self.assertEqual(result.text, "abcde")
        self.assertTrue(result.text_truncated)
        self.assertTrue(result.truncated)

    def test_invalid_limits_are_rejected_before_network(self):
        for kwargs in (
            {"timeout": 0},
            {"max_bytes": 0},
            {"max_chars": 0},
            {"max_links": -1},
            {"max_redirects": -1},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    fetch("http://example.com/", **kwargs)
        self.open_mock.assert_not_called()


class ExtractionTests(WebFetchTestCase):
    HTML = """<!doctype html>
<html>
  <head>
    <title> Test &amp; Page </title>
    <style>body { color: red; }</style>
    <script>alert("script-text")</script>
  </head>
  <body>
    <noscript>noscript-text</noscript>
    <nav>nav-text</nav>
    <main>
      <h1>Heading</h1>
      <p>Hello <b>world</b>.</p>
      <p>Second paragraph.</p>
      <a href="/one"> One </a>
      <a href="two.html#frag">Two</a>
      <a href="javascript:alert(1)">bad</a>
      <a href="mailto:x@y.z">mail</a>
      <a href="https://other.example/x">Other</a>
    </main>
    <footer>footer-text</footer>
  </body>
</html>"""

    def _fetch_html(self, html=None, **kwargs):
        body = (html if html is not None else self.HTML).encode("utf-8")
        self.use_open(
            FakeResponse(body=body, content_type="text/html; charset=utf-8")
        )
        return fetch("http://example.com/dir/page", **kwargs)

    def test_extracts_title_text_and_absolute_links(self):
        result = self._fetch_html()
        self.assertEqual(result.title, "Test & Page")
        self.assertIn("Heading", result.text)
        self.assertIn("Hello world.", result.text)
        self.assertIn("Second paragraph.", result.text)
        self.assertEqual(
            [link["url"] for link in result.links],
            [
                "http://example.com/one",
                "http://example.com/dir/two.html",
                "https://other.example/x",
            ],
        )
        self.assertEqual(result.links[0]["text"], "One")
        for link in result.links:
            self.assertTrue(link["url"].startswith("http"))

    def test_strips_scripts_styles_noscript_and_boilerplate(self):
        result = self._fetch_html()
        for forbidden in (
            "script-text",
            "color: red",
            "noscript-text",
            "nav-text",
            "footer-text",
        ):
            self.assertNotIn(forbidden, result.text)
        self.assertNotIn("script-text", result.title)

    def test_skips_non_http_and_credentialed_links(self):
        html = """<main>
          <a href="#top">top</a>
          <a href="javascript:alert(1)">js</a>
          <a href="mailto:x@y.z">mail</a>
          <a href="tel:+123">tel</a>
          <a href="data:text/plain,hi">data</a>
          <a href="http://user:pass@example.com/x">creds</a>
          <a href="/ok">ok</a>
        </main>"""
        result = self._fetch_html(html)
        self.assertEqual(
            [link["url"] for link in result.links],
            ["http://example.com/ok"],
        )

    def test_links_are_bounded_and_flagged(self):
        anchors = "".join(
            f'<a href="/link-{index}">Link {index}</a>'
            for index in range(150)
        )
        result = self._fetch_html(f"<main>{anchors}</main>", max_links=10)
        self.assertEqual(len(result.links), 10)
        self.assertTrue(result.links_truncated)
        self.assertTrue(result.truncated)

    def test_duplicate_links_are_collapsed(self):
        result = self._fetch_html(
            '<main><a href="/same">A</a><a href="/same">B</a></main>'
        )
        self.assertEqual(len(result.links), 1)
        self.assertEqual(result.links[0]["text"], "A")

    def test_text_is_bounded_and_flagged(self):
        result = self._fetch_html(
            "<main><p>" + ("word " * 100) + "</p></main>", max_chars=40
        )
        self.assertLessEqual(len(result.text), 40)
        self.assertTrue(result.text_truncated)
        self.assertTrue(result.truncated)

    def test_empty_html(self):
        result = self._fetch_html("")
        self.assertEqual(result.title, "")
        self.assertEqual(result.text, "")
        self.assertEqual(result.links, [])
        self.assertFalse(result.truncated)

    def test_no_raw_html_by_default_and_opt_in(self):
        result = self._fetch_html()
        self.assertIsNone(result.html)
        self.assertNotIn("html", result.to_dict())
        result_with_html = self._fetch_html(include_html=True)
        self.assertIsNotNone(result_with_html.html)
        self.assertIn("<html>", result_with_html.html)
        self.assertIn("html", result_with_html.to_dict())

    def test_result_is_json_serializable(self):
        result = self._fetch_html()
        payload = json.dumps(result.to_dict(), ensure_ascii=False)
        restored = json.loads(payload)
        self.assertEqual(restored["final_url"], result.final_url)
        self.assertEqual(restored["links"], result.links)
        self.assertIsInstance(restored["truncated"], bool)

    def test_request_headers_are_minimal(self):
        request = fetch_module._build_request("http://example.com/")
        headers = {key.lower(): value for key, value in request.headers.items()}
        self.assertIn("Misty", headers["user-agent"])
        self.assertNotIn("cookie", headers)
        self.assertNotIn("authorization", headers)
        self.assertEqual(request.get_method(), "GET")


if __name__ == "__main__":
    unittest.main()
